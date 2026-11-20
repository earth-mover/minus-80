"""Check a copy made by transfer.py against earthmover-public/camelyon.

``OME`` and ``documents`` are compared in full. A random sample of slides is
compared by :func:`minus80.core.repo_copy.check`: every ``zarr.json``, and the
references or stored bytes of a sample of arrays. Then one JPEG tile from the
middle of each sampled slide's full-resolution image is decoded from both
repos, which reads the CAMELYON bucket through the copied references. The run
takes about a minute.
"""
from __future__ import annotations

import argparse
import asyncio
import random

import numpy as np
import zarr

import transfer
from transfer import PREFIX, open_source, open_target
from minus80.core.repo_copy import check, units_at


def decode_tile(src, dst, slide: str) -> str:
    path = f"{slide}/image/0"
    a = zarr.open_array(src, mode="r", path=path)
    b = zarr.open_array(dst, mode="r", path=path)
    _, height, width = a.shape
    y = height // 2 // 512 * 512
    x = width // 2 // 512 * 512
    selection = (slice(None), slice(y, y + 512), slice(x, x + 512))
    tile = a[selection]
    if not np.array_equal(tile, b[selection]):
        raise AssertionError(f"{path}: decoded tile differs")
    return f"{path} rows {y}-{y + 512}, columns {x}-{x + 512}"


async def run(args) -> None:
    _, src = open_source()
    dst = open_target(args.target).readonly_session("main").store
    _, units = await units_at(src, transfer.DEPTH)
    slides = [u for u in units if u.rpartition("/")[2] not in ("OME", "documents")]
    if args.only:
        slides = [f"CAMELYON16/{name}" for name in args.only.split(",")]
    for unit in ("CAMELYON16/OME", "CAMELYON16/documents"):
        await check(src, dst, unit, sample=1000)
    sampled = random.Random(args.seed).sample(slides, min(args.slides, len(slides)))
    for slide in sampled:
        await check(src, dst, slide, args.sample)
        tile = await asyncio.to_thread(decode_tile, src, dst, slide)
        print(f"  {slide}: decodes identically: {tile}", flush=True)
    print(f"verified {len(sampled)} of {len(slides)} slides against {PREFIX}", flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default=transfer.ARRAYLAKE + transfer.TARGET)
    ap.add_argument("--slides", type=int, default=8, help="slides to compare")
    ap.add_argument("--sample", type=int, default=12, help="arrays per slide whose chunks are compared")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--only", help="comma-separated slides to draw the sample from, "
                                   "for a target that holds only those")
    asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
