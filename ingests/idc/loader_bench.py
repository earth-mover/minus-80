"""Time reads the way a training loader makes them: random level-0 tiles, with their mask tiles.

Each sample is one tile-aligned window of ``image/0`` on a random slide, and
for TCGA the same window of ``labels/nuclei/0``. For TCGA, samples are
drawn from tiles whose mask holds a nucleus, as a loader that skips
background would; for GTEx, from all tiles. The samples are read on a
thread pool of 1 (the first 64 only), 16 and 64 threads.

    uv run --package idc-ingest python ingests/idc/loader_bench.py REPO [--samples 256]
"""

from __future__ import annotations

import argparse
import random
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from minus80.core.icechunk_io import open_repo



def slides(root) -> list[str]:
    return [path for path, node in root.members(max_depth=3) if path.count("/") == 2]


def tissue_tiles(slide, rng: random.Random, n: int) -> list[tuple[int, int]]:
    level0 = slide["image/0"]
    ty, tx = level0.chunks[1:]
    rows, cols = -(-level0.shape[1] // ty), -(-level0.shape[2] // tx)
    if "labels" in slide["image"]:
        mask = slide["image/labels/nuclei/0"]
        sample = [(rng.randrange(rows), rng.randrange(cols)) for _ in range(20 * n)]
        stored = [rc for rc in sample if mask.get_block_selection(rc).any()]
        return stored[:n]
    return [(rng.randrange(rows), rng.randrange(cols)) for _ in range(n)]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--samples", type=int, default=256)
    a = p.parse_args()
    root = open_repo(a.repo)
    rng = random.Random(1)
    paths = slides(root)
    per = a.samples // len(paths) + 1
    work = []
    for path in paths:
        slide = root[path]
        image = slide["image/0"]
        mask = slide["image/labels/nuclei/0"] if "labels" in slide["image"] else None
        work += [(image, mask, rc) for rc in tissue_tiles(slide, rng, per)]
    rng.shuffle(work)
    work = work[:a.samples]

    def read(item):
        image, mask, rc = item
        t = time.perf_counter()
        tile = image.get_block_selection((0, *rc))
        m = mask.get_block_selection(rc) if mask is not None else None
        return time.perf_counter() - t, tile.nbytes + (0 if m is None else m.nbytes)

    for workers in (1, 16, 64):
        sample = work if workers > 1 else work[:64]
        start = time.perf_counter()
        with ThreadPoolExecutor(workers) as pool:
            out = list(pool.map(read, sample))
        wall = time.perf_counter() - start
        lat = np.array([t for t, _ in out]) * 1e3
        print(f"workers {workers:3d}: {len(sample)} pairs in {wall:.2f} s = {len(sample) / wall:6.1f} pairs/s; "
              f"latency median {np.median(lat):.0f} ms, p90 {np.percentile(lat, 90):.0f} ms")


if __name__ == "__main__":
    main()
