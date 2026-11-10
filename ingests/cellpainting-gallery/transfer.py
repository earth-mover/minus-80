"""Copy the head of earthmover-public/cellpainting-gallery's main into another repo.

The repo is laid out ``<dataset>/<source>/<batch>/<plate>``. Each child of a
batch (a plate, or the batch's ``plates`` group) is one unit of
:mod:`minus80.core.repo_copy`, and each batch is one commit.

    uv run --package cellpainting-gallery-ingest python \\
        ingests/cellpainting-gallery/transfer.py --target arraylake://<org>/<repo>
"""
from __future__ import annotations

import argparse
import asyncio

from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo
from minus80.core.repo_copy import copy_units, units_at

SOURCE = "arraylake://earthmover-public/cellpainting-gallery"
PREFIX = "s3://cellpainting-gallery/"
DEPTH = 4


def open_target(location: str):
    """The repo to write: an existing Arraylake repo, or a new local or S3 one."""
    return create_repo(location, [], prefixes=[PREFIX], open_existing=True)


async def batch_children(store) -> tuple[list[str], list[str]]:
    """The groups above the batches' children, and the children themselves."""
    return await units_at(store, DEPTH)


async def run(args) -> None:
    source = arraylake_repository(SOURCE)
    snapshot = source.lookup_branch("main")
    src = source.readonly_session(snapshot_id=snapshot).store
    parents, units = await batch_children(src)
    if args.only:
        wanted = args.only.split(",")
        if missing := set(wanted) - set(units):
            raise SystemExit(f"not in {SOURCE}: {sorted(missing)}")
        units = wanted
    origin = f"{SOURCE.removeprefix(ARRAYLAKE)} snapshot {snapshot}"
    print(f"copying {len(units)} subtrees of {origin} to {args.target}")
    await copy_units(src, open_target(args.target), parents, units, origin, args.concurrency)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", required=True,
                    help="Icechunk repo: a directory, an s3:// URL, or arraylake://<org>/<repo>")
    ap.add_argument("--only", help="comma-separated batch children to copy; default all")
    ap.add_argument("--concurrency", type=int, default=64)
    asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
