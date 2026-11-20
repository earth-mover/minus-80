"""Copy the head of earthmover-public/camelyon's main into another repo.

The slide pixels are references to JPEG tiles in the public CAMELYON bucket.
They are declared again in the target, so no pixel is copied; only the
annotations, documents and derived mask levels, which the repo stores itself,
are copied as bytes. Each group below ``CAMELYON16`` (``OME``, ``documents``
and one per slide) is a unit of :mod:`minus80.core.repo_copy`, and units are
committed in batches so that a stopped run keeps the batches it finished.

    uv run --package camelyon-ingest python ingests/camelyon/transfer.py \\
        --target arraylake://earthmover-biology/camelyon
"""
from __future__ import annotations

import argparse
import asyncio

from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo
from minus80.core.repo_copy import copy_units, units_at

SOURCE = "earthmover-public/camelyon"
TARGET = "earthmover-biology/camelyon"
PREFIX = "s3://camelyon-dataset/"

#: Units below the root: the challenge group, then its members.
DEPTH = 2

#: Slides per commit. A commit costs a round trip per array it writes, and a
#: batch that fails again loses at most this many slides of work.
BATCH = 50


def open_target(location: str):
    """The repo to write: an existing Arraylake repo, or a new local or S3 one."""
    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[PREFIX])
    return create_repo(location, [], prefixes=[PREFIX], open_existing=True)


def open_source():
    """The source repo and a read-only store at the head of its main."""
    repo = arraylake_repository(ARRAYLAKE + SOURCE)
    snapshot = repo.lookup_branch("main")
    return snapshot, repo.readonly_session(snapshot_id=snapshot).store


async def run(args) -> None:
    snapshot, src = open_source()
    parents, units = await units_at(src, DEPTH)
    if args.only:
        wanted = [f"CAMELYON16/{name}" for name in args.only.split(",")]
        if missing := set(wanted) - set(units):
            raise SystemExit(f"not in {SOURCE}: {sorted(missing)}")
        units = wanted
    target = open_target(args.target)
    origin = f"{SOURCE} snapshot {snapshot}"
    print(f"copying {len(units)} units of {origin} to {args.target}", flush=True)
    for start in range(0, len(units), BATCH):
        await copy_units(src, target, parents, units[start:start + BATCH], origin, args.concurrency)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default=ARRAYLAKE + TARGET,
                    help="Icechunk repo: a directory, an s3:// URL, or arraylake://<org>/<repo>")
    ap.add_argument("--only", help="comma-separated units below CAMELYON16 to copy, such as "
                                   "OME,documents,tumor_056; default all")
    ap.add_argument("--concurrency", type=int, default=64)
    asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
