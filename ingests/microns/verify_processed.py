"""Spot-check phase 2 of the MICrONS store: the NWB files, the ROI masks and the EM matches.

``test_processed.py`` checks the code on a small fixture. This script checks
what depends on the real archive, at main and at every phase-2 commit:

- ``complete``: every scan has its NWB group, an NWBFile, and one ``rois-<n>``
  image per field whose ROI count equals the NWB table's and ``units``'.
  Metadata only.
- ``codecs``: every array's codec chain, at every phase-2 commit, uses only
  codecs zarr-python decodes with nothing of ours or shadowzarr's installed:
  Zarr's core codecs and numcodecs' zlib. shadowzarr's ``keep_bytes`` fails it.
- ``images``: the ``rois-<n>`` images' OME-Zarr documents validate, and each
  is translated to where the scene places its field.
- ``sources``: in the first two scans asked for, one ROI's mask, one trace,
  one summary image and one stimulus frame equal h5py's decode of the NWB
  file, and xarray opens the trace's series group.
- ``matches``: at each ``cave-v<version>`` commit, 100 sampled matched units
  of each table hold the root ID, supervoxel and EM point of that version's
  dump, and the commit lists exactly the dump's match tables.

    MINUS80_S3_ANONYMOUS=1 uv run --package microns-ingest python ingests/microns/verify_processed.py \\
        --repo <dir | arraylake://org/repo> [--scans 4-7,9-6] [--tag]

With ``--tag``, and only when every check passes, the processed commit is
tagged ``processed`` and each CAVE commit ``cave-v<version>``.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

import h5py
import ingest
import numpy as np
import processed
import xarray as xr
import zarr
from ingest import SCANS

from minus80.core import remote
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, open_repository
from minus80.ome.image import validate

#: The codecs a reader with only zarr-python installed decodes: Zarr's core
#: codecs and numcodecs' zlib, which zarr-python depends on.
READABLE_CODECS = {"bytes", "transpose", "sharding_indexed", "crc32c", "blosc", "zstd", "gzip",
                   "vlen-utf8", "numcodecs.zlib"}


def codec_names(codecs: list[dict]) -> set[str]:
    names = set()
    for codec in codecs:
        names.add(codec["name"])
        configuration = codec.get("configuration") or {}
        for key in ("codecs", "index_codecs"):
            names |= codec_names(configuration.get(key, []))
    return names


def codecs(root) -> dict[tuple, str]:
    """Each codec chain in the store, with one array that uses it."""
    seen = {}
    for path, node in root.members(max_depth=None):
        if isinstance(node, zarr.Array):
            chain = tuple(sorted(codec_names(node.metadata.to_dict()["codecs"])))
            seen.setdefault(chain, path)
    return seen


def complete(root, names: list[str]) -> list[str]:
    problems = []
    for name in names:
        scan = root[f"{SCANS}/{name}"]
        if dict(scan["nwb"].attrs).get("neurodata_type") != "NWBFile":
            problems.append(f"{name}: nwb is not an NWBFile")
        counts = {int(k): v for k, v in dict(scan["units"].attrs)["rois_per_field"].items()}
        fields = sorted(int(k.removeprefix("field-")) for k in scan.group_keys() if k.startswith("field-"))
        if sorted(counts) != fields:
            problems.append(f"{name}: units name fields {sorted(counts)}, the scan has {fields}")
        for n in fields:
            table = scan[f"nwb/processing/ophys/ImageSegmentation/PlaneSegmentation{n}/id"]
            image = scan[f"rois-{n}/0"]
            if not (table.shape[0] == image.shape[0] == counts.get(n)):
                problems.append(f"{name} field {n}: NWB {table.shape[0]} ROIs, rois-{n} {image.shape[0]}, "
                                f"units {counts.get(n)}")
        if scan["units/unit_id"].shape[0] != sum(counts.values()):
            problems.append(f"{name}: units has {scan['units/unit_id'].shape[0]} entries")
    return problems


def images(root, names: list[str]) -> list[str]:
    problems = []
    for name in names:
        scan = root[f"{SCANS}/{name}"]
        scene = {t["input"]["path"]: t["translation"]
                 for t in dict(scan.attrs)["ome"]["scene"]["coordinateTransformations"]}
        for key in sorted(k for k in scan.group_keys() if k.startswith("rois-")):
            attrs = dict(scan[key].attrs)
            try:
                validate(attrs["ome"], ingest.VERSION, model="image", strict=True)
            except Exception as exc:  # noqa: BLE001 -- reported as a failed check
                problems.append(f"{name}/{key}: {type(exc).__name__}: {exc}"[:160])
            (multiscale,) = attrs["ome"]["multiscales"]
            transforms = [step for t in multiscale["datasets"][0]["coordinateTransformations"]
                          for step in (t["transformations"] if t["type"] == "sequence" else [t])]
            translation = next(t["translation"] for t in transforms if t["type"] == "translation")
            if not np.allclose(translation[1:], scene[key.replace("rois", "field")][1:]):
                problems.append(f"{name}/{key}: placed at {translation[1:]}, its field at "
                                f"{scene[key.replace('rois', 'field')][1:]}")
    return problems


def sources(root, scan: ingest.Scan, rng: random.Random) -> list[str]:
    problems = []
    group = root[f"{SCANS}/{scan.name}"]
    counts = {int(k): v for k, v in dict(group["units"].attrs)["rois_per_field"].items()}
    n = rng.choice(sorted(counts))
    roi = rng.randrange(counts[n])
    ophys = "processing/ophys"
    with h5py.File(remote.reader(scan.nwb, block=1 << 20, cached_blocks=32), "r") as f:
        pairs = [
            ("mask", group[f"rois-{n}/0"][roi, 0],
             f[f"{ophys}/ImageSegmentation/PlaneSegmentation{n}/image_mask"][roi].T),
            ("trace", group[f"nwb/{ophys}/Fluorescence/RoiResponseSeries{n}/data"][:, roi],
             f[f"{ophys}/Fluorescence/RoiResponseSeries{n}/data"][:, roi]),
            ("average", group[f"nwb/{ophys}/SegmentationImages{n}/average"][:],
             f[f"{ophys}/SegmentationImages{n}/average"][()]),
        ]
        video = next(k for k in f["acquisition"] if k.startswith("Video"))
        frame = rng.randrange(f[f"acquisition/{video}/data"].shape[0])
        pairs.append(("stimulus frame", group[f"nwb/acquisition/{video}/data"][frame],
                      f[f"acquisition/{video}/data"][frame]))
    for what, ours, theirs in pairs:
        if ours.shape != theirs.shape or ours.tobytes() != theirs.tobytes():
            problems.append(f"{scan.name} field {n} ROI {roi + 1}: the {what} differs from the NWB file")
    series = xr.open_zarr(group.store, group=f"{group.path}/nwb/{ophys}/Fluorescence/RoiResponseSeries{n}",
                          consolidated=False, zarr_format=3)
    if "time" not in series["data"].dims:
        problems.append(f"{scan.name}: xarray sees the trace's axes as {series['data'].dims}")
    return problems


def matches(repo, snapshot, scans: list[str], rng: random.Random, base: str = processed.CAVE_DUMPS) -> list[str]:
    """At one CAVE commit: each scan holds the dump's tables that have rows for it, and sampled rows agree.

    Each array is read once per table and scan, and the sampled units are
    taken from it, so a check over Arraylake costs a few reads per table.
    """
    version = snapshot.metadata["cave_version"]
    tables = processed.dump_tables(version, base)
    root = zarr.open_group(repo.readonly_session(snapshot_id=snapshot.id).store, mode="r")
    problems = []
    rows_by_table = {}
    for table, (frame, _) in tables.items():
        rows = frame.drop_duplicates(subset=[c for c in frame.columns if c != "id"])
        rows_by_table[table] = rows.assign(scan=rows.session.astype(str) + "-" + rows.scan_idx.astype(str))
    for name in scans:
        em = root[f"{SCANS}/{name}/units/em"]
        want_tables = {t for t, rows in rows_by_table.items() if (rows.scan == name).any()}
        if set(em.group_keys()) != want_tables:
            problems.append(f"v{version} {name}: tables {sorted(em.group_keys())}, "
                            f"the dump has rows in {sorted(want_tables)}")
    for table, rows in rows_by_table.items():
        rows = rows[rows.scan.isin(scans)]
        sample = rows.sample(min(100, len(rows)), random_state=rng.randrange(2**31))
        for name, picked in sample.groupby("scan"):
            group = root[f"{SCANS}/{name}/units/em/{table}"]
            roots, supervoxels, points = group["pt_root_id"][:], group["pt_supervoxel_id"][:], group["pt_position"][:]
            at = picked.unit_id.to_numpy() - 1
            want_points = picked[["pt_position_x", "pt_position_y", "pt_position_z"]].to_numpy()
            bad = ((roots[at] != picked.pt_root_id.to_numpy().astype("uint64"))
                   | (supervoxels[at] != picked.pt_supervoxel_id.to_numpy().astype("uint64"))
                   | (points[at] != want_points).any(axis=1))
            if bad.any():
                k = int(np.flatnonzero(bad)[0])
                problems.append(f"v{version} {table} {name} unit {at[k] + 1}: root {roots[at[k]]}, "
                                f"the dump {picked.pt_root_id.iloc[k]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--scans", help="scans to check, comma-separated; default all in the store")
    ap.add_argument("--tag", action="store_true", help="tag the phase-2 commits if every check passes")
    args = ap.parse_args(argv)
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    started = time.monotonic()
    repo = (arraylake_repository(args.repo) if args.repo.startswith(ARRAYLAKE)
            else open_repository(args.repo, source_prefixes=[ingest.SOURCE, processed.DANDI_BLOBS]))
    steps = processed.committed(repo)
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    names = args.scans.split(",") if args.scans else sorted(root[SCANS].group_keys())
    scans = {s.name: s for s in ingest.list_scans() if s.name in names}
    rng = random.Random(0)
    failures = 0

    def report(check: str, problems: list[str]) -> None:
        nonlocal failures
        failures += bool(problems)
        print(f"{'FAIL' if problems else 'ok  '} {check} ({time.monotonic() - started:.0f} s)", flush=True)
        for p in problems[:10]:
            print(f"     {p}", flush=True)

    report("complete", complete(root, names))
    chains = {}
    for snapshot in steps.values():
        chains |= codecs(zarr.open_group(repo.readonly_session(snapshot_id=snapshot.id).store, mode="r"))
    report("codecs", [f"{path}: {sorted(set(chain) - READABLE_CODECS)}"
                      for chain, path in chains.items() if not set(chain) <= READABLE_CODECS])
    print(f"     codec chains: {sorted(chains)}", flush=True)
    report("images", images(root, names))
    report("sources", sum((sources(root, scans[n], rng) for n in names[:2]), []))
    cave = sorted((s for k, s in steps.items() if k.startswith("cave-")),
                  key=lambda s: s.metadata["cave_version"])
    report("matches", sum((matches(repo, s, names, rng) for s in cave), []))
    missing = [v for v in processed.CAVE_VERSIONS if f"cave-v{v}" not in steps]
    report("versions", [f"no commit for CAVE v{v}" for v in missing] + (
        [] if processed.PROCESSED in steps else ["no processed commit"]))
    print(f"{'passed' if not failures else f'{failures} checks failed'} in "
          f"{time.monotonic() - started:.0f} s", flush=True)
    if failures:
        return 1
    if args.tag:
        held = set(repo.list_tags())
        for step, snapshot in steps.items():
            if step in held:
                if repo.lookup_tag(step) != snapshot.id:
                    print(f"tag {step} names {repo.lookup_tag(step)}, not {snapshot.id}", flush=True)
                    return 1
                continue
            repo.create_tag(step, snapshot_id=snapshot.id)
            print(f"tagged {step} = {snapshot.id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
