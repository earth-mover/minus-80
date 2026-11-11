"""Measure the ingest's two write paths: one decoded morphology plane and one copied label mask.

PLAN.md, "Steps, scope and risk", step 2. Each part writes under the root group ``measure/`` of a
scratch branch, in one commit. Each part prints wall time, CPU time, bytes read, bytes written and
its commit time, then checks what it wrote against the bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import zarr
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, create_repo
from minus80.core.progress import Progress
from minus80.core.pyramid import check_page, create_level, write_page
from minus80.core.zarr_copy import copy_chunks, create_copy
from zarr.storage import LoggingStore

from bundle import CELLS, MORPHOLOGY_2D
from ingest import FIXTURE_LEVEL, MASKS, MIB, open_bundle
from sources import S3, SAMPLES
from verify import chunk_key, chunk_members, raw

PLANE = MORPHOLOGY_2D[0]
MASK = MASKS["nucleus_labels"]
CHECK_TILES = 4
CHECK_CHUNKS = 20
LOCAL_CHUNKS = 8


class Clock:
    """Wall time and the CPU time of all of the process's threads, from creation."""

    def __init__(self):
        self.wall, self.cpu = time.monotonic(), time.process_time()

    def read(self) -> tuple[float, float]:
        return time.monotonic() - self.wall, time.process_time() - self.cpu


class TimedWrites:
    """An array whose item writes add up their own wall time and advance a progress line."""

    def __init__(self, array: zarr.Array, progress: Progress):
        self.array, self.progress, self.seconds = array, progress, 0.0

    def __setitem__(self, region, value) -> None:
        start = time.monotonic()
        self.array[region] = value
        self.seconds += time.monotonic() - start
        self.progress.advance()

    def __getattr__(self, name):
        return getattr(self.array, name)


class Chunks:
    """The chunk members ``names`` of the zip ``zf``, and nothing else, for ``copy_chunks``."""

    def __init__(self, zf, names: list[str]):
        self.zf, self.names = zf, names

    def namelist(self) -> list[str]:
        return self.names

    def read(self, name: str) -> bytes:
        return self.zf.read(name)


def sampled(items: list, n: int) -> list:
    """``n`` items spread evenly over ``items``, first and last included."""
    return [items[i] for i in np.unique(np.linspace(0, len(items) - 1, min(n, len(items))).astype(int))]


def counting_root(session) -> tuple[zarr.Group, LoggingStore]:
    """The session's root group, through a store that counts its calls and logs nothing."""
    store = LoggingStore(session.store, log_level="WARNING")
    return zarr.open_group(store, mode="a"), store


def finish(repo, session, store, clock, path: str, counter, before: int, message: str) -> dict:
    """Commit ``session`` and return the part's times, byte counts and store calls."""
    wall, cpu = clock.read()
    calls = dict(store.counter)
    start = time.monotonic()
    snapshot = commit(session, message)
    seconds = time.monotonic() - start
    array = zarr.open_array(repo.readonly_session(snapshot_id=snapshot).store, path=path, mode="r")
    return {
        "path": path, "snapshot": snapshot, "wall_s": wall, "cpu_s": cpu,
        "commit_s": seconds, "read_bytes": counter.bytes - before,
        "stored_bytes": array.nbytes_stored(), "store_gets": calls.get("get", 0),
        "store_sets": calls.get("set", 0),
    }


def measure_plane(repo, branch: str, group: str, page, counter) -> dict:
    """Decode ``page`` and write it as one plane through ``write_page``, in one commit."""
    session = repo.writable_session(branch)
    root, store = counting_root(session)
    # One chunk per source tile, as the ingest writes; a tile then never fills part of a chunk.
    array = create_level(root.require_group(group), "morphology", (1, *page.shape), page.dtype,
                         dims=("c", "y", "x"), chunk=(page.tilelength, page.tilewidth))
    tiles = len(page.dataoffsets)
    before, clock = counter.bytes, Clock()
    # Counted from here, so the array's metadata write is not in the chunk writes.
    store.counter.clear()
    with Progress(f"{array.path}: decoding {tiles:,} tiles", total=tiles, unit="tiles") as progress:
        target = TimedWrites(array, progress)
        written = write_page(target, page, (0,))
    result = finish(repo, session, store, clock, array.path, counter, before, f"Measure {array.path}")
    decoded = int(np.prod(page.shape)) * page.dtype.itemsize
    result |= {
        "tiles": written, "shape": list(page.shape), "decoded_bytes": decoded,
        "write_s": target.seconds, "decode_threads": page.maxworkers,
        "tiles_per_s": written / result["wall_s"], "decoded_mib_per_s": decoded / MIB / result["wall_s"],
    }
    return result


def measure_mask(repo, branch: str, group: str, zf, prefix: str, counter, limit: int | None) -> dict:
    """Copy the chunks of the v2 array ``prefix`` byte for byte through ``copy_chunks``, in one commit."""
    names = chunk_members(zf, prefix)[:limit]
    sizes = {i.filename: i.compress_size for i in zf.infolist()}
    session = repo.writable_session(branch)
    root, store = counting_root(session)
    array = create_copy(root.require_group(group), "nucleus_mask", json.loads(zf.read(f"{prefix}/.zarray")),
                        dimension_names=("y", "x"))
    before, clock = counter.bytes, Clock()
    store.counter.clear()
    total = sum(sizes[n] for n in names)
    with Progress(f"{array.path}: copying {len(names):,} chunks", total=total, unit="MiB", scale=MIB) as progress:
        counter.progress = progress
        try:
            copied = copy_chunks(Chunks(zf, names), prefix, array)
        finally:
            counter.progress = None
    result = finish(repo, session, store, clock, array.path, counter, before, f"Measure {array.path}")
    result |= {
        "chunks": copied, "chunk_bytes": total,
        "chunks_per_s": copied / result["wall_s"], "mib_per_s": total / MIB / result["wall_s"],
    }
    return result


def check_plane(repo, result: dict, page) -> list[str]:
    """Compare ``CHECK_TILES`` sampled tiles of the written plane with tifffile's decode."""
    array = zarr.open_array(repo.readonly_session(snapshot_id=result["snapshot"]).store, path=result["path"])
    rows, cols = (-(-n // t) for n, t in zip(page.shape, (page.tilelength, page.tilewidth)))
    tiles = sampled([(r, c) for r in range(rows) for c in range(cols)], CHECK_TILES)
    print(f"  check: {len(tiles)} of {rows * cols:,} tiles: {tiles}", flush=True)
    return check_page(array, page, (0,), tiles)


def check_mask(repo, result: dict, zf, prefix: str) -> list[str]:
    """Compare ``CHECK_CHUNKS`` sampled chunks of the copied mask with the source members, byte for byte."""
    store = repo.readonly_session(snapshot_id=result["snapshot"]).store
    names = sampled(chunk_members(zf, prefix)[:result["chunks"]], CHECK_CHUNKS)
    print(f"  check: {len(names)} of {result['chunks']:,} chunks", flush=True)
    return [f"{n} differs" for n in names if raw(store, chunk_key(result["path"], prefix, n)) != zf.read(n)]


def report(name: str, result: dict, errors: list[str]) -> None:
    print(f"{name}: {'PASS' if not errors else 'FAIL'}", flush=True)
    for error in errors[:20]:
        print(f"  {error}")
    shown = {k: round(v, 3) if isinstance(v, float) else v for k, v in result.items()}
    print(f"result {name}: {json.dumps(shown)}", flush=True)


def open_target(args):
    if args.local:
        location = Path(args.local)
        return create_repo(location, [], open_existing=location.exists())
    return arraylake_repository(args.repo)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--repo", help="arraylake://<org>/<repo>; it must exist")
    target.add_argument("--local", help=f"a local repo directory; level {FIXTURE_LEVEL} and {LOCAL_CHUNKS} chunks")
    parser.add_argument("--branch", default=f"measure-{datetime.now(timezone.utc):%Y-%m-%d}")
    parser.add_argument("--sample", choices=sorted(SAMPLES), default="kidney")
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    parser.add_argument("--parts", nargs="+", choices=("plane", "mask"), default=["plane", "mask"])
    args = parser.parse_args()
    if args.repo and not args.repo.startswith(ARRAYLAKE):
        raise SystemExit("--repo must be arraylake://<org>/<repo>")
    if args.branch in ("main", "fixture"):
        raise SystemExit(f"--branch {args.branch} is not a scratch branch")
    level, limit = (FIXTURE_LEVEL, LOCAL_CHUNKS) if args.local else (0, None)

    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    print(f"cpus: {os.cpu_count()}, usable by this process: {affinity}", flush=True)
    bundle, counter = open_bundle(args.bundle or SAMPLES[args.sample].url("_outs.zip", S3))
    repo = open_target(args)
    if args.branch not in repo.list_branches():
        repo.create_branch(args.branch, repo.lookup_branch("main"))
    # One group per run, so a rerun on the same branch never meets an earlier run's arrays.
    group = f"measure/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    print(f"writing {group} on branch {args.branch}", flush=True)

    failed = False
    if "plane" in args.parts:
        page = bundle.tiff(PLANE).series[0].levels[level].pages[0]
        result = measure_plane(repo, args.branch, group, page, counter)
        errors = check_plane(repo, result, page)
        report("plane", {"source": f"{PLANE} level {level}", **result}, errors)
        failed |= bool(errors)
    if "mask" in args.parts:
        zf = bundle.member_zip(CELLS)
        result = measure_mask(repo, args.branch, group, zf, MASK, counter, limit)
        errors = check_mask(repo, result, zf, MASK)
        report("mask", {"source": f"{CELLS} {MASK}", **result}, errors)
        failed |= bool(errors)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
