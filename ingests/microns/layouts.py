"""Record the layout of every scan TIFF, as the TIFF parser reads it.

    uv run --package microns-ingest python ingests/microns/layouts.py <layouts.json> [--parallel 3]

For each of the 38 TIFFs, parses every directory with
``TiffParser(prefetch="directories")`` and writes one row: page count, page
shape and dtype, the first page's offset, whether the pages are contiguous
and of one size, where the directories sit, and the time the fetch and parse
took. ``diagrams.py`` and PLAN.md take their per-file numbers from the
output. It fetches 3.1 GB of directories in all, so run it beside the bucket
or expect several minutes from elsewhere.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import time

import numpy as np

import ingest
from plumbing import list_prefix


def layout(url: str) -> dict:
    from minus80.core.registry import registry_for
    from minus80.parsers.tiff import TiffParser
    from minus80.parsers.tiff.inspect import directory_layout

    registry = registry_for(url, patient=True)
    started = time.perf_counter()
    found = directory_layout(url, registry=registry)
    probed = time.perf_counter()
    pages = TiffParser(prefetch="directories")(url, registry).group["0"]
    parsed = time.perf_counter()
    # The manifest's grid is (pages, 1, 1): one strip per page.
    offsets = np.array([pages.manifest.get_entry((i, 0, 0))["offset"] for i in range(pages.shape[0])])
    lengths = np.array([pages.manifest.get_entry((i, 0, 0))["length"] for i in range(pages.shape[0])])
    row = {
        "name": url.rsplit("/", 1)[1], "size": found.size, "pages": int(pages.shape[0]),
        "shape": list(pages.shape), "dtype": str(pages.dtype),
        "page_bytes": sorted({int(x) for x in np.unique(lengths)}),
        "first_offset": int(offsets[0]), "pixels_end": int(offsets[-1] + lengths[-1]),
        "contiguous": bool(np.all(offsets[1:] == offsets[:-1] + lengths[:-1])),
        "first_directory": found.first_directory, "second_directory": found.second_directory,
        "stride": found.stride, "entries": found.entries,
        "region_bytes": found.region[1] - found.region[0],
        "layout_s": round(probed - started, 2), "fetch_and_parse_s": round(parsed - probed, 2),
    }
    print(json.dumps(row), flush=True)
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("output")
    ap.add_argument("--parallel", type=int, default=3,
                    help="files parsed at once; each parse holds about 1.5 GB")
    args = ap.parse_args()
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    urls = [ingest.SOURCE + name for name in sorted(list_prefix(ingest.SOURCE))
            if ingest.TIFF_NAME.match(name)]
    print(f"{len(urls)} TIFFs", flush=True)
    with multiprocessing.get_context("spawn").Pool(args.parallel) as pool:
        rows = pool.map(layout, urls, chunksize=1)
    with open(args.output, "w") as f:
        json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
