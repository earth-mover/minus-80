"""Check a finished CHAMMI-75 store against the source, in about a minute.

The ingest's tests check every plane and every cell of a fixture. This
script checks what depends on the real archive, making a few requests per
study:

  licenses  Every study in the store is allowlisted and states its license;
            index/ holds no row of any other study.
  complete  Each study holds every image and PNG the metadata lists for it,
            read from the work directory's local copy of the metadata.
  gates     minus80's OME-Zarr gates: schema, axes, the collection's series
            and a source on every virtual array.
  pixels    One chunk per study, read through the store, equals PIL's decode
            of the PNG fetched from the bucket. The PNG is found from the
            metadata by image ID and slot, not from the store.
  cells     One image per study with positions: its cells and background,
            read through the store, equal its highest channel's file in the
            zip, where the ingest read the lowest.

    uv run --package chammi-75-ingest python ingests/chammi-75/verify.py \\
        --repo <icechunk dir or arraylake://org/repo> [--work <dir>] [--checks pixels,cells]
"""

from __future__ import annotations

import argparse
import io
import os
import random
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import obstore
import pandas as pd
import zarr
from PIL import Image
from safetensors.numpy import load as load_safetensors

from minus80.core.fetch import cache_dir, read_range
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, open_repository
from minus80.ome.verify import GATES, run_gates
from minus80.parsers.zip.directory import parse_member

import pipeline
from ingest import INDEX_COLUMNS, LICENSES, NO_BACKGROUND, ROOT, guidance_name

CHECKS = ["licenses", "complete", "gates", "pixels", "cells"]


def open_store(location: str, branch: str):
    repo = arraylake_repository(location) if location.startswith(ARRAYLAKE) else open_repository(location)
    return zarr.open_group(repo.readonly_session(branch).store, mode="r")


def load_index(root) -> pd.DataFrame:
    return pd.DataFrame({k: root[f"index/{k}"][:] for k in INDEX_COLUMNS})


def check_licenses(root, index: pd.DataFrame) -> list[str]:
    problems = []
    studies = sorted(set(root.group_keys()) - {"OME", "index", "cells"})
    for study in studies:
        stated = dict(root[study].attrs).get("annotations", {}).get("license")
        if study not in LICENSES:
            problems.append(f"{study}: in the store but not allowlisted")
        elif stated != LICENSES[study]:
            problems.append(f"{study}: states license {stated!r}, the allowlist says {LICENSES[study]!r}")
    if dict(root.attrs).get("licenses") != ROOT["licenses"]:
        problems.append("the root's license table differs from the allowlist")
    if leaked := sorted(set(index["study"]) - LICENSES.keys()):
        problems.append(f"index/ holds rows of studies that are not allowlisted: {leaked}")
    wrong = index["license"] != index["study"].map(LICENSES)
    if wrong.any():
        problems.append(f"index/license disagrees with the allowlist on {int(wrong.sum()):,} rows")
    if sorted(index["study"].unique()) != studies:
        problems.append(f"index/ studies {sorted(index['study'].unique())} differ from the groups {studies}")
    print(f"  {len(studies)} studies, {len(index):,} images", flush=True)
    return problems


def check_complete(root, index: pd.DataFrame, metadata: Path) -> list[str]:
    """Each study's images and PNGs against the metadata; a trial store's sample against its first images."""
    studies = sorted(index["study"].unique())
    sample = dict(root.attrs).get("contents", {}).get("sample")
    keep = "" if sample is None else f"where rank <= {int(sample)}"
    listed = duckdb.sql(f"""
        with m as (
          select "experiment.study" study, "imaging.multi_channel_id" image,
                 dense_rank() over (partition by "experiment.study" order by "imaging.multi_channel_id") rank
          from read_parquet('{metadata}') where "experiment.study" in ({",".join(f"'{s}'" for s in studies)}))
        select study, count(distinct image) images, count(*) pngs from m {keep}
        group by 1""").df().set_index("study")
    problems = []
    for study in studies:
        rows = index[index["study"] == study]
        stacks = rows.drop_duplicates("stack")
        pngs = sum(root[f"{s}/ch{slot}/0"].shape[0] for s, channels in
                   stacks[["stack", "channels"]].itertuples(index=False) for slot in channels.split("-"))
        expected = listed.loc[study]
        if len(rows) != expected["images"] or pngs != expected["pngs"]:
            problems.append(f"{study}: {len(rows):,} images and {pngs:,} PNGs stored, the metadata "
                            f"lists {expected['images']:,} and {expected['pngs']:,}")
    print(f"  {int(listed['pngs'].sum()):,} PNGs listed for {len(studies)} studies", flush=True)
    return problems


def png_keys(metadata: Path, image_ids: list[str]) -> dict[tuple[str, int], str]:
    """Each (image ID, slot)'s PNG key, from the metadata."""
    ids = ",".join(f"'{i}'" for i in image_ids)
    rows = duckdb.sql(f"""select "imaging.multi_channel_id", cast(cast("imaging.channel" as double) as int),
                                 "storage.path"
                          from read_parquet('{metadata}') where "imaging.multi_channel_id" in ({ids})""").fetchall()
    return {(image, slot): key for image, slot, key in rows}


def check_pixels(root, index: pd.DataFrame, metadata: Path, archive: str) -> list[str]:
    rng = random.Random(0)
    picks = []
    for _, rows in index.groupby("study"):
        r = rows.iloc[rng.randrange(len(rows))]
        picks.append((r["stack"], int(r["row"]), r["image_id"], int(rng.choice(r["channels"].split("-")))))
    keys = png_keys(metadata, [p[2] for p in picks])
    store = pipeline.archive_store(archive)
    problems = []
    for stack, row, image, slot in picks:
        stored = root[f"{stack}/ch{slot}/0"][row]
        source = np.asarray(Image.open(io.BytesIO(bytes(obstore.get(store, keys[(image, slot)]).bytes()))))
        if not np.array_equal(stored, source):
            problems.append(f"{stack}/ch{slot}/0[{row}] differs from {keys[(image, slot)]}")
    print(f"  {len(picks)} chunks, one per study", flush=True)
    return problems


def check_cells(root, index: pd.DataFrame, metadata: Path, archive: str, members: Path) -> list[str]:
    rng = random.Random(2)
    picks = [rows.iloc[rng.randrange(len(rows))]
             for _, rows in index[index["has_positions"]].groupby("study")]
    keys = png_keys(metadata, [p["image_id"] for p in picks])
    highest = {p["image_id"]: guidance_name(keys[(p["image_id"], int(p["channels"].split("-")[-1]))])
               for p in picks}
    names = ",".join(f"'{n}'" for n in highest.values())
    spans = {r["name"]: r for r in duckdb.sql(
        f"select * from read_parquet('{members}') where name in ({names})").to_arrow_table().to_pylist()}
    cells = root["cells"]
    problems = []
    for pick in picks:
        entry = spans[highest[pick["image_id"]]]
        span = read_range(archive + pipeline.GUIDANCE_ZIP, entry["start"], entry["end"], patient=True)
        tensors = load_safetensors(parse_member(span, entry["start"], entry))
        xy, sizes = tensors["data"].reshape(-1, 2), tensors.get("sizes")
        i = int(pick.name)
        background = cells["background"][i]
        if pick["background_dropped"]:
            same = np.array_equal(background, xy[0])
            xy, sizes = xy[1:], (sizes[1:] if sizes is not None else None)
        else:
            same = bool((background == NO_BACKGROUND).all())
        lo, hi = cells["image_start"][i:i + 2]
        same &= np.array_equal(cells["position"][lo:hi], xy)
        same &= np.array_equal(cells["area"][lo:hi], sizes if sizes is not None else np.zeros(len(xy)))
        if not same:
            problems.append(f"{pick['image_id']}: cells differ from {entry['name']}")
    print(f"  {len(picks)} images' cells, one per study with positions", flush=True)
    return problems


def verify(location: str, branch: str, archive: str, work: Path, checks: list[str]) -> dict[str, list[str]]:
    root = open_store(location, branch)
    index = load_index(root)
    metadata = work / f"{Path(pipeline.METADATA_CSV).stem}.parquet"
    members = work / "guidance-members.parquet"
    failures = {}
    for check in checks:
        started = time.monotonic()
        print(check, flush=True)
        if check == "licenses":
            problems = check_licenses(root, index)
        elif check == "complete":
            problems = check_complete(root, index, metadata)
        elif check == "gates":
            failed = run_gates(root, GATES, {"version": "0.6", "sample": 0})
            problems = [f"{failed} gate(s) failed"] if failed else []
        elif check == "pixels":
            problems = check_pixels(root, index, metadata, archive)
        elif check == "cells":
            problems = check_cells(root, index, metadata, archive, members)
        else:
            raise ValueError(f"unknown check {check}")
        print(f"  {'FAIL' if problems else 'pass'} {check} in {time.monotonic() - started:.1f} s", flush=True)
        for problem in problems[:10]:
            print(f"    {problem}", flush=True)
        failures[check] = problems
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--archive", default=pipeline.ARCHIVE)
    parser.add_argument("--work", type=Path, default=Path(cache_dir()) / "chammi-75",
                        help="the ingest's work directory, holding the metadata and the zip's members")
    parser.add_argument("--checks", default=",".join(CHECKS), help=f"comma-separated, from {CHECKS}")
    args = parser.parse_args(argv)
    if args.archive.startswith("s3://"):
        os.environ["MINUS80_S3_ANONYMOUS"] = "1"
    failures = verify(args.repo, args.branch, args.archive, args.work, args.checks.split(","))
    failed = [k for k, v in failures.items() if v]
    print("all checks pass" if not failed else f"failed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
