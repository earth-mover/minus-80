"""Check a written RxRx19 store against the zips, in about a minute.

    uv run --package rxrx19-ingest python ingests/rxrx19/verify.py \\
        --repo <dir | arraylake://earthmover-biology/RxRx19> --dataset rxrx19a [--branch main] [--sites 5]

The store is right by design: every node is built from metadata.csv's rows by
ingest.node_documents, in one place. So the check is small and its cost does
not grow with the store:

- **Plates.** Every plate metadata.csv lists is in the store, and its plate
  document names exactly the wells metadata.csv gives it: one document per
  plate, 57 or 53 reads.
- **Documents.** One plate, one well and one site validate against the
  OME-Zarr schema.
- **Pixels.** ``--sites`` sites, drawn across the dataset's plates, each read
  through Icechunk at one channel and compared with the PNG decoded straight
  from the zip: two requests per site.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import imagecodecs
import numpy as np
import zarr

import layout
from ingest import open_repo, plates_to_write
from layout import DATASETS
from minus80.ome.image import validate
from minus80.parsers.zip.reading import read_members
from source import rebased, survey


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--plates", help="only these plates (HRCE-1/1,…), for a partial store such as a trial")
    parser.add_argument("--sites", type=int, default=5)
    parser.add_argument("--source-root")
    parser.add_argument("--cache", type=Path, help="where the central directories are kept (default: the minus80 cache)")
    args = parser.parse_args(argv)

    d = DATASETS[args.dataset]
    if args.source_root:
        d = rebased(d, args.source_root)
    s = survey(d, args.cache)
    store = zarr.open_group(open_repo(args.repo, d).readonly_session(args.branch).store, mode="r")
    problems = []
    started = time.monotonic()

    plates = plates_to_write(s, args.plates)
    for experiment, plate in plates:
        path = layout.plate_path(d.key, experiment, plate)
        if path not in store:
            problems.append(f"{path}: missing")
            continue
        expected = {"/".join(layout.split_well(w)) for w in
                    s.metadata[(s.metadata.experiment == experiment) & (s.metadata.plate == plate)].well}
        stored = {w["path"] for w in store[path].attrs["ome"]["plate"]["wells"]}
        if stored != expected:
            problems.append(f"{path}: {len(stored ^ expected)} wells differ from metadata.csv")
    print(f"plates: {len(plates)} checked ({time.monotonic() - started:.1f} s)", flush=True)

    experiment, plate = plates[0]
    first = s.metadata[(s.metadata.experiment == experiment) & (s.metadata.plate == plate)].iloc[0]
    for path, model, strict in [
        (layout.plate_path(d.key, experiment, plate), "plate", False),
        (layout.well_path(d.key, experiment, plate, first.well), "well", False),
        (layout.site_path(d.key, experiment, plate, first.well, int(first.site)), "image", True),
    ]:
        try:
            validate(store[path].attrs["ome"], layout.OME_VERSION, model=model, strict=strict)
        except Exception as exc:
            problems.append(f"{path}: {type(exc).__name__}: {exc}"[:160])
    print(f"documents: a plate, a well and a site validated ({time.monotonic() - started:.1f} s)", flush=True)

    rng = random.Random(19)
    chosen = {f"{e}_{p}" for e, p in plates}
    candidates = s.pngs[(s.pngs.experiment + "_" + s.pngs.plate.astype(str)).isin(chosen)]
    for _ in range(args.sites):
        png = candidates.iloc[rng.randrange(len(candidates))]
        array = store[f"{layout.site_path(d.key, png.experiment, int(png.plate), png.well, int(png.site))}/0"]
        source = imagecodecs.png_decode(read_members(layout.readable(d.images), [png["name"]], s.directory)[png["name"]])
        if not np.array_equal(array[int(png.channel) - 1], source):
            problems.append(f"{png['name']}: the stored chunk differs from the PNG")
    print(f"pixels: {args.sites} sites compared with their PNGs ({time.monotonic() - started:.1f} s)", flush=True)

    for problem in problems:
        print(f"  FAIL  {problem}")
    print("pass" if not problems else f"{len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
