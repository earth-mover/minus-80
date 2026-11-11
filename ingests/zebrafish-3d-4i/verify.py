"""Spot-check a converted 3D-4i zebrafish store against the zip it came from.

``test_ingest.py`` checks the conversion itself, on a scaled-down plate where
every check is cheap. This script checks only what depends on the real plate,
in about a minute and a few dozen requests to EBI:

- ``complete``: every well, round and label the zip's central directory names
  is in the store. The directory is the converter's local Parquet copy, so this
  makes no request to EBI.
- ``schema``: every OME-Zarr document validates against the 0.6 schema.
- ``pixels``: in one image of each of two wells, the middle plane of one
  channel and of each label at level 0 equals the source's.

    uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/verify.py \\
        --repo <dir | arraylake://org/repo>
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import ingest
import numpy as np
import zarr
from pipeline import open_repository

from minus80.ome.verify import run_gates, schema
from minus80.parsers.fractal.plate import FIELDS_TABLE, TABLES
from minus80.parsers.zip.reading import cached_central_directory, read_central_directory
from minus80.parsers.zip.store import MemberStore
from minus80.parsers.zip.zarr_v2 import frame_problem, is_metadata


def unreadable_chunk(name: str, data: bytes) -> str | None:
    """Why a source chunk cannot be decoded, by the converter's own test, or None.

    The same test as the converter, so a chunk the source cannot decode reads
    as the fill value on both sides of a comparison.
    """
    if is_metadata(name) or f"/{TABLES}/" in name:
        return None
    return frame_problem(data, {"id": "blosc"})


def open_source(url: str, directory_cache: Path | None):
    """The zip's central directory, and the plate as a zarr group that fetches each member as it is read.

    With no ``directory_cache`` the directory is read from the zip. Sharing the
    converter's copy cannot hide a stale offset: every member read checks that
    its local header names that member and that its bytes match the CRC.
    """
    if directory_cache is None:
        directory = read_central_directory(url)
    else:
        directory = cached_central_directory(url, directory_cache, check_version=False)
    store = MemberStore(url, directory, check=unreadable_chunk)
    return directory, zarr.open_group(store, mode="r", zarr_format=2)


def held(directory) -> dict[str, dict[int, set[str]]]:
    """Each well's rounds, with the labels each round holds, from member names alone."""
    wells = defaultdict(lambda: defaultdict(set))
    for name in directory["name"].to_pylist():
        parts = name.split("/")
        if len(parts) < 4 or not parts[2].isdigit():
            continue
        labels = wells[f"{parts[0]}/{parts[1]}"][int(parts[2])]
        if len(parts) >= 6 and parts[3] == "labels" and parts[5] == ".zattrs":
            labels.add(parts[4])
    return {well: dict(rounds) for well, rounds in wells.items()}


def plate_wells(root) -> list[str]:
    return [w["path"] for w in dict(root.attrs)["ome"]["plate"]["wells"]]


def well_images(root, well: str) -> list[str]:
    return [f"{well}/{i['path']}" for i in dict(root[well].attrs)["ome"]["well"]["images"]]


def complete(root, source: dict[str, dict[int, set[str]]]) -> list[str]:
    """Every well, round and label the source holds is in the store."""
    stored = plate_wells(root)
    if missing := sorted(set(source) - set(stored)):
        return [f"the plate lacks the source's wells {missing}"]
    problems = []
    for well in stored:
        rounds = sorted(source[well])
        labels = sorted(set().union(*source[well].values()))
        for name in well_images(root, well):
            image = root[name]
            got = sorted({c["round"] for c in dict(image.attrs)["annotations"]["channels"]})
            if got != rounds:
                problems.append(f"{name}: rounds {got}, the source's well holds {rounds}")
            got = sorted(dict(image["labels"].attrs)["ome"]["labels"]) if "labels" in image else []
            if got != labels:
                problems.append(f"{name}: labels {got}, the source's well holds {labels}")
    return problems


def pixels(root, plate, source: dict[str, dict[int, set[str]]], *, wells: int = 2, seed: int = 0) -> list[str]:
    """The middle plane of one channel and of each label equals the source's, at level 0.

    One image in each of ``wells`` wells. The field's window in the source's
    well image comes from the ROI row and the pixel size the store records, so
    a wrong ROI or pixel size fails here too.
    """
    rng = random.Random(seed)
    problems = []
    for well in sorted(rng.sample(plate_wells(root), wells)):
        name = rng.choice(well_images(root, well))
        image = root[name]
        attrs = dict(image.attrs)
        roi = attrs["annotations"][FIELDS_TABLE]
        (multiscale,) = attrs["ome"]["multiscales"]
        (transform,) = multiscale["datasets"][0]["coordinateTransformations"]
        steps = transform["transformations"] if transform["type"] == "sequence" else [transform]
        scale = next(t["scale"] for t in steps if t["type"] == "scale")
        level = image["0"]
        y0, x0 = round(roi["y_micrometer"] / scale[2]), round(roi["x_micrometer"] / scale[3])
        window = (slice(y0, y0 + level.shape[2]), slice(x0, x0 + level.shape[3]))
        z = level.shape[1] // 2
        stored = rng.randrange(level.shape[0])
        channel = attrs["annotations"]["channels"][stored]
        want = plate[f"{well}/{channel['round']}/0"][channel["source_channel"], z, *window]
        if not np.array_equal(level[stored, z], want):
            problems.append(f"{name}/0 channel {stored} ({channel['label']}), plane {z}: differs from the source")
        labels = {label: rnd for rnd, names in source[well].items() for label in names}
        for label, rnd in sorted(labels.items()):
            want = plate[f"{well}/{rnd}/labels/{label}/0"][z, *window]
            if not np.array_equal(image[f"labels/{label}/0"][z], want):
                problems.append(f"{name}/labels/{label}/0, plane {z}: differs from the source")
        print(f"          {name}: channel {channel['label']} and {len(labels)} labels, plane {z}", flush=True)
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--source", default=ingest.SOURCE)
    parser.add_argument("--directory-cache", type=Path, default=ingest.DIRECTORY_CACHE)
    args = parser.parse_args()
    started = time.monotonic()
    root = zarr.open_group(
        open_repository(args.repo).readonly_session(args.branch).store, mode="r"
    )
    directory, plate = open_source(args.source, args.directory_cache)
    source = held(directory)
    failed = run_gates(
        root,
        {
            "complete": lambda root, n: complete(root, source),
            "schema": lambda root, n: schema(root, ingest.VERSION),
            "pixels": lambda root, n: pixels(root, plate, source),
        },
        {},
    )
    print(f"{'FAILED' if failed else 'passed'} in {time.monotonic() - started:.0f} s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
