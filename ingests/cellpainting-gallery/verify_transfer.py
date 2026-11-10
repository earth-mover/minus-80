"""Check a copy made by transfer.py against earthmover-public/cellpainting-gallery.

On every node of each subtree given, the ``zarr.json`` must match the source's
byte for byte. On a random sample of arrays, every chunk must match: the same
location, offset and length for a virtual chunk, the same stored bytes for a
native or inline one. One level-0 field per subtree is then decoded from both
repos and compared.
"""
from __future__ import annotations

import argparse
import asyncio

import numpy as np
import zarr

from transfer import SOURCE, batch_children, open_target
from minus80.core.icechunk_io import arraylake_repository
from minus80.core.repo_copy import check


def decode_one(src_session, dst_session, unit: str) -> None:
    src_group = zarr.open_group(src_session.store, mode="r", path=unit)
    level0 = None
    for path, node in src_group.members(max_depth=None):
        if isinstance(node, zarr.Array) and path.endswith("/0") and node.ndim >= 3:
            level0 = f"{unit}/{path}"
            break
    if level0 is None:
        print(f"  {unit}: no level-0 array to decode", flush=True)
        return
    a = zarr.open_array(src_session.store, mode="r", path=level0)[:]
    b = zarr.open_array(dst_session.store, mode="r", path=level0)[:]
    if not np.array_equal(a, b):
        raise AssertionError(f"{level0}: decoded pixels differ")
    print(f"  {unit}: {level0} decodes identically, shape {a.shape}", flush=True)


async def run(args) -> None:
    source = arraylake_repository(SOURCE)
    src_session = source.readonly_session(snapshot_id=source.lookup_branch("main"))
    dst_session = open_target(args.target).readonly_session("main")
    units = args.units or (await batch_children(src_session.store))[1]
    for unit in units:
        await check(src_session.store, dst_session.store, unit, args.sample)
        await asyncio.to_thread(decode_one, src_session, dst_session, unit)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", required=True)
    ap.add_argument("--sample", type=int, default=50, help="arrays per subtree whose chunks are compared")
    ap.add_argument("units", nargs="*", help="batch children to check; default all")
    asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
