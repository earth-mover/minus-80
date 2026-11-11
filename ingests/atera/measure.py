"""Measure the ingest's write paths on a subset of the work: a plane, a mask, a stack column, the H&E and one tile.

PLAN.md, "Steps, scope and risk", step 2. Each run does one ``--part`` and prints one JSON line last.
A store part writes under ``measure/<part>-c<concurrency>/`` of a ``measure-`` branch, in one commit.
``--part all`` runs every part in its own process, so each part's peak memory is its own.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import zarr
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, create_repo
from minus80.core.progress import MIB, Progress
from minus80.core.pyramid import check_page, create_level, write_page, write_stack
from minus80.core.registry import as_url
from minus80.core.remote import reader
from minus80.core.tables import row_group_bounds
from minus80.core.zarr_copy import copy_chunks, create_copy
from zarr.storage import LoggingStore

from bundle import CELLS, MORPHOLOGY_2D, MORPHOLOGY_3D, TRANSCRIPTS, open_tiff
from ingest import MASKS, member_bytes, open_bundle
from sources import HE_IMAGE, S3, SAMPLES
from tables_extra import codewords_table
from transcripts import TRANSCRIPT_SCHEMA, write_tile
from verify import chunk_key, chunk_members, raw

PLANE = MORPHOLOGY_2D[0]
MASK = MASKS["nucleus_labels"]
CHECK_TILES = 4
CHECK_CHUNKS = 20
PARTS = ("plane", "mask", "stack", "he", "tile")
#: The work of each part, from the part's source: tiles, chunks, shard positions, tiles.
LIMITS = {"plane": 400, "mask": 400, "stack": 8, "he": 200}
BRANCH_PREFIX = "measure-"


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
        self._lock = threading.Lock()

    def __setitem__(self, region, value) -> None:
        start = time.monotonic()
        self.array[region] = value
        with self._lock:
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


class FirstTiles:
    """A page that yields only its first ``tiles`` segments, for ``write_page``, which takes no subset."""

    def __init__(self, page, tiles: int):
        self._page, self._tiles = page, tiles

    def segments(self, *args, **kwargs):
        segments = self._page.segments(*args, **kwargs)
        try:
            yield from itertools.islice(segments, self._tiles)
        finally:
            segments.close()

    def __getattr__(self, name):
        return getattr(self._page, name)


class Corner:
    """A page cut to its first ``length`` rows and ``width`` columns, for ``write_stack``, which takes no subset.

    ``write_stack`` decodes by tile index. The index is right while the cut keeps the first tile row.
    """

    def __init__(self, page, length: int, width: int):
        self._page, self.imagelength, self.imagewidth = page, length, width

    def __getattr__(self, name):
        return getattr(self._page, name)


def grid_tiles(page) -> int:
    """The tiles of one plane of ``page``."""
    return -(-page.imagelength // page.tilelength) * -(-page.imagewidth // page.tilewidth)


def sampled(items: list, n: int) -> list:
    """``n`` items spread evenly over ``items``, first and last included."""
    return [items[i] for i in np.unique(np.linspace(0, len(items) - 1, min(n, len(items))).astype(int))]


def counting_root(session) -> tuple[zarr.Group, LoggingStore]:
    """The session's root group, through a store that counts its calls and logs nothing."""
    store = LoggingStore(session.store, log_level="WARNING")
    return zarr.open_group(store, mode="a"), store


def finish(repo, session, store, clock, path: str, counter, before: tuple[int, int], message: str) -> dict:
    """Commit ``session`` and return the part's times, source reads, stored bytes and store calls."""
    wall, cpu = clock.read()
    calls = dict(store.counter)
    start = time.monotonic()
    snapshot = commit(session, message)
    seconds = time.monotonic() - start
    array = zarr.open_array(repo.readonly_session(snapshot_id=snapshot).store, path=path, mode="r")
    return {
        "path": path, "snapshot": snapshot, "wall_s": wall, "cpu_s": cpu, "commit_s": seconds,
        "read_bytes": counter.bytes - before[0], "source_requests": counter.requests - before[1],
        "stored_bytes": array.nbytes_stored(), "store_gets": calls.get("get", 0), "store_sets": calls.get("set", 0),
    }


def rated(result: dict, count: int, unit: str) -> dict:
    return result | {"count": count, "unit": unit, "rate": count / result["wall_s"], "rate_unit": f"{unit}/s"}


def measure_plane(repo, branch: str, group: str, page, counter, *, tiles: int, concurrency: int = 1,
                  name: str = "morphology") -> dict:
    """Decode the first ``tiles`` tiles of ``page`` and write them through ``write_page``, in one commit."""
    session = repo.writable_session(branch)
    root, store = counting_root(session)
    samples = page.samplesperpixel
    # One chunk per source tile, as the ingest writes; a tile then never fills part of a chunk.
    array = create_level(root.require_group(group), name, (samples, page.imagelength, page.imagewidth), page.dtype,
                         dims=("c", "y", "x"), chunk=(page.tilelength, page.tilewidth))
    before, clock = (counter.bytes, counter.requests), Clock()
    # Counted from here, so the array's metadata write is not in the chunk writes.
    store.counter.clear()
    with Progress(f"{array.path}: decoding {tiles:,} tiles", total=tiles, unit="tiles") as progress:
        target = TimedWrites(array, progress)
        written = write_page(target, FirstTiles(page, tiles), (0,) if samples == 1 else (), concurrency=concurrency)
    result = finish(repo, session, store, clock, array.path, counter, before, f"Measure {array.path}")
    return rated(result | {"write_s": target.seconds}, written, "tiles")


def first_positions(pages, positions: int) -> list[Corner]:
    """``pages`` cut to the first ``positions`` shard positions of their first tile row."""
    first = pages[0]
    return [Corner(p, min(first.tilelength, first.imagelength), min(positions * first.tilewidth, first.imagewidth))
            for p in pages]


def measure_stack(repo, branch: str, group: str, pages, counter, *, positions: int, concurrency: int = 1) -> dict:
    """Write the first ``positions`` shard positions of the stack through ``write_stack``, in one commit."""
    session = repo.writable_session(branch)
    root, store = counting_root(session)
    first, corner = pages[0], first_positions(pages, positions)
    array = create_level(root.require_group(group), "morphology_3d",
                         (1, len(pages), corner[0].imagelength, corner[0].imagewidth), first.dtype,
                         dims=("c", "z", "y", "x"), chunk=(first.tilelength, first.tilewidth), shard_planes=len(pages))
    before, clock = (counter.bytes, counter.requests), Clock()
    store.counter.clear()
    with Progress(f"{array.path}: {positions} shard positions of {len(pages)} planes", total=positions,
                  unit="positions") as progress:
        target = TimedWrites(array, progress)
        written = write_stack(target, corner, (0,), concurrency=concurrency)
    result = finish(repo, session, store, clock, array.path, counter, before, f"Measure {array.path}")
    return rated(result | {"write_s": target.seconds, "planes": len(pages)}, written, "positions")


def measure_mask(repo, branch: str, group: str, zf, prefix: str, counter, *, limit: int, concurrency: int = 1) -> dict:
    """Copy ``limit`` chunks of the v2 array ``prefix`` byte for byte through ``copy_chunks``, in one commit."""
    names = chunk_members(zf, prefix)[:limit]
    sizes = {i.filename: i.compress_size for i in zf.infolist()}
    session = repo.writable_session(branch)
    root, store = counting_root(session)
    array = create_copy(root.require_group(group), "nucleus_mask", json.loads(zf.read(f"{prefix}/.zarray")),
                        dimension_names=("y", "x"))
    before, clock = (counter.bytes, counter.requests), Clock()
    store.counter.clear()
    total = sum(sizes[n] for n in names)
    with Progress(f"{array.path}: copying {len(names):,} chunks", total=total, unit="MiB", scale=MIB) as progress:
        counter.progress = progress
        try:
            copied = copy_chunks(Chunks(zf, names), prefix, array, concurrency=concurrency)
        finally:
            counter.progress = None
    result = finish(repo, session, store, clock, array.path, counter, before, f"Measure {array.path}")
    return rated(result | {"write_s": None, "chunk_bytes": total}, copied, "chunks")


def field_ids(schema: pa.Schema) -> dict[str, int]:
    """A field id for each dotted column name, struct children included; no table registers the file."""
    names = []
    for field in schema:
        names.append(field.name)
        if pa.types.is_struct(field.type):
            names += [f"{field.name}.{child.name}" for child in field.type]
    return {name: i for i, name in enumerate(names, 1)}


def measure_tile(zf, grid: dict, tile: str, counter, path: Path) -> dict:
    """Write ``grid/<tile>`` to the Parquet file ``path`` with ``write_tile``; no table, no repo."""
    before, clock = (counter.bytes, counter.requests), Clock()
    codewords = codewords_table(zf)
    with Progress(f"tile {tile}: {grid[tile]:,} rows", total=member_bytes(zf, f"grid/{tile}/"), unit="MiB",
                  scale=MIB) as progress:
        counter.progress = progress
        try:
            rows = write_tile(zf, tile, codewords, path, first_instance=0, field_ids=field_ids(TRANSCRIPT_SCHEMA))
        finally:
            counter.progress = None
    wall, cpu = clock.read()
    return rated({
        "path": str(path), "snapshot": None, "wall_s": wall, "cpu_s": cpu, "commit_s": None,
        "read_bytes": counter.bytes - before[0], "source_requests": counter.requests - before[1],
        "stored_bytes": path.stat().st_size, "parquet_bytes": path.stat().st_size, "store_gets": 0,
        "store_sets": 0, "write_s": None,
    }, rows, "rows")


def tile_counts(zf) -> dict[str, int]:
    """The row count of each transcript tile, from ``grid/.zattrs``."""
    grid = json.loads(zf.read("grid/.zattrs"))
    return dict(zip(grid["grid_keys"], grid["grid_number_objects"], strict=True))


def check_plane(repo, result: dict, page, tiles: int) -> list[str]:
    """Compare ``CHECK_TILES`` sampled written tiles of the plane with tifffile's decode."""
    array = zarr.open_array(repo.readonly_session(snapshot_id=result["snapshot"]).store, path=result["path"])
    per_row = -(-page.imagewidth // page.tilewidth)
    sample = sampled([divmod(i, per_row) for i in range(tiles)], CHECK_TILES)
    print(f"  check: {len(sample)} of {tiles:,} tiles: {sample}", flush=True)
    return check_page(array, page, (0,) if page.samplesperpixel == 1 else (), sample)


def check_stack(repo, result: dict, pages, positions: int) -> list[str]:
    """Compare ``CHECK_TILES`` sampled shard positions, in every plane, with tifffile's decode."""
    array = zarr.open_array(repo.readonly_session(snapshot_id=result["snapshot"]).store, path=result["path"])
    sample = sampled([(0, col) for col in range(positions)], CHECK_TILES)
    print(f"  check: {len(sample)} of {positions} positions in {len(pages)} planes", flush=True)
    return [f"plane {z}: {e}" for z, page in enumerate(first_positions(pages, positions)) for e in check_page(array, page, (0, z), sample)]


def check_mask(repo, result: dict, zf, prefix: str) -> list[str]:
    """Compare ``CHECK_CHUNKS`` sampled chunks of the copied mask with the source members, byte for byte."""
    store = repo.readonly_session(snapshot_id=result["snapshot"]).store
    names = sampled(chunk_members(zf, prefix)[:result["count"]], CHECK_CHUNKS)
    print(f"  check: {len(names)} of {result['count']:,} chunks", flush=True)
    return [f"{n} differs" for n in names if raw(store, chunk_key(result["path"], prefix, n)) != zf.read(n)]


def check_tile(path: Path, rows: int) -> list[str]:
    """The Parquet file at ``path`` holds ``rows`` rows and its ``instance`` footer runs from 0 to ``rows - 1``."""
    bounds = row_group_bounds(path, "instance")
    found, low, high = sum(n for _, _, n in bounds), min(b[0] for b in bounds), max(b[1] for b in bounds)
    errors = [f"the file has {found} rows, the source {rows}"] if found != rows else []
    return errors + ([f"instance runs {low}..{high}, not 0..{rows - 1}"] if (low, high) != (0, rows - 1) else [])


def peak_memory_mib() -> float:
    """The process's peak resident memory; ``ru_maxrss`` is bytes on macOS and kibibytes on Linux."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1 << 20 if sys.platform == "darwin" else 1 << 10)


def report(args, result: dict, errors: list[str]) -> None:
    """Print the part's verdict, then one JSON line with the part's numbers."""
    print(f"{args.part}: {'PASS' if not errors else 'FAIL'}", flush=True)
    for error in errors[:20]:
        print(f"  {error}")
    final = {
        "part": args.part, "concurrency": args.concurrency, "limit": args.limit, **result,
        "peak_rss_mib": peak_memory_mib(), "machine": platform.machine(), "cpu_count": os.cpu_count(),
        "check": "PASS" if not errors else "FAIL",
    }
    print(json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in final.items()}), flush=True)


def open_target(args):
    if args.local:
        location = Path(args.local)
        return create_repo(location, [], open_existing=location.exists())
    return arraylake_repository(args.repo)


def run_part(args) -> int:
    """Run ``args.part`` once in this process; returns the exit code."""
    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    print(f"cpus: {os.cpu_count()}, usable by this process: {affinity}", flush=True)
    sample = SAMPLES[args.sample]
    bundle, counter = open_bundle(args.bundle or sample.url("_outs.zip", S3))
    if args.part == "tile":
        zf = bundle.member_zip(TRANSCRIPTS)
        counts = tile_counts(zf)
        tile = args.tile or max(counts, key=counts.get)
        if tile not in counts:
            raise SystemExit(f"the source has no tile {tile}")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(args.scratch or temporary) / f"tile-{tile}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            result = measure_tile(zf, {tile: counts[tile]}, tile, counter, path)
            errors = check_tile(path, counts[tile])
        report(args, {"source": f"{TRANSCRIPTS} grid/{tile}", **result}, errors)
        return int(bool(errors))

    repo = open_target(args)
    if args.branch not in repo.list_branches():
        repo.create_branch(args.branch, repo.lookup_branch("main"))
    group = f"measure/{args.part}-c{args.concurrency}"
    print(f"writing {group} on branch {args.branch}", flush=True)
    n = args.concurrency
    if args.part == "plane":
        page = bundle.tiff(PLANE).series[0].levels[0].pages[0]
        tiles = min(args.limit, grid_tiles(page))
        result = measure_plane(repo, args.branch, group, page, counter, tiles=tiles, concurrency=n)
        errors, source = check_plane(repo, result, page, tiles), f"{PLANE} level 0"
    elif args.part == "he":
        he_url = args.he_image or sample.url(HE_IMAGE, S3)
        # The object is read as write_he reads it, through the counter.
        page = open_tiff(reader(as_url(he_url), wrap=counter.wrap)).series[0].levels[0].pages[0]
        tiles = min(args.limit, grid_tiles(page))
        result = measure_plane(repo, args.branch, group, page, counter, tiles=tiles, concurrency=n, name="he_image")
        errors, source = check_plane(repo, result, page, tiles), f"{HE_IMAGE} level 0"
    elif args.part == "stack":
        pages = list(bundle.tiff(MORPHOLOGY_3D).series[0].levels[0].pages)
        per_row = -(-pages[0].imagewidth // pages[0].tilewidth)
        positions = min(args.limit, per_row)
        result = measure_stack(repo, args.branch, group, pages, counter, positions=positions, concurrency=n)
        errors, source = check_stack(repo, result, pages, positions), f"{MORPHOLOGY_3D} level 0"
    else:
        zf = bundle.member_zip(CELLS)
        result = measure_mask(repo, args.branch, group, zf, MASK, counter, limit=args.limit, concurrency=n)
        errors, source = check_mask(repo, result, zf, MASK), f"{CELLS} {MASK}"
    report(args, {"source": source, **result}, errors)
    return int(bool(errors))


def run_all(args) -> int:
    """Run each part for each concurrency in its own process; returns 1 if any part fails."""
    failed = False
    for concurrency, part in itertools.product(args.concurrencies, PARTS):
        if part == "tile" and concurrency != args.concurrencies[0]:
            continue
        command = [sys.executable, __file__, "--part", part, "--branch", args.branch, "--sample", args.sample]
        if part != "tile":
            command += ["--concurrency", str(concurrency)]
        for flag in ("local", "repo", "bundle", "he_image", "scratch"):
            if getattr(args, flag):
                command += [f"--{flag.replace('_', '-')}", getattr(args, flag)]
        failed |= subprocess.run(command).returncode != 0
    return int(failed)


def parse(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--repo", help="arraylake://<org>/<repo>; it must exist")
    target.add_argument("--local", help="a local repo directory")
    parser.add_argument("--branch", required=True, help=f"a scratch branch; its name starts with {BRANCH_PREFIX}")
    parser.add_argument("--sample", choices=sorted(SAMPLES), default="kidney")
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    parser.add_argument("--he-image", help="the H&E OME-TIFF, a URL or a local path; by default 10x's object")
    parser.add_argument("--part", choices=(*PARTS, "all"), required=True)
    parser.add_argument("--limit", type=int, help=f"the part's work, by default {LIMITS}; tile takes none")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1],
                        help="writes at once; several values only with --part all")
    parser.add_argument("--tile", help="the transcript tile, by default the one with the most rows")
    parser.add_argument("--scratch", help="the directory of the tile's Parquet file, by default a temporary one")
    args = parser.parse_args(argv)
    if not args.branch.startswith(BRANCH_PREFIX):
        raise SystemExit(f"--branch {args.branch} is not a scratch branch; its name starts with {BRANCH_PREFIX}")
    if args.repo and not args.repo.startswith(ARRAYLAKE):
        raise SystemExit("--repo must be arraylake://<org>/<repo>")
    if args.part != "all" and len(args.concurrency) != 1:
        raise SystemExit("--concurrency takes one value unless --part is all")
    if args.part == "all" and args.limit is not None:
        raise SystemExit("--limit does not apply to --part all; each part uses its own default")
    if args.part == "tile" and (args.limit is not None or args.concurrency != [1]):
        raise SystemExit("--part tile takes neither --limit nor --concurrency")
    if args.part != "all" and args.part != "tile" and any(n < 1 for n in args.concurrency):
        raise SystemExit("--concurrency is at least 1")
    args.concurrencies, args.concurrency = args.concurrency, args.concurrency[0]
    if args.part in LIMITS and args.limit is None:
        args.limit = LIMITS[args.part]
    if args.part == "tile":
        args.concurrency = None
    return args


def main() -> None:
    args = parse()
    raise SystemExit(run_all(args) if args.part == "all" else run_part(args))


if __name__ == "__main__":
    main()
