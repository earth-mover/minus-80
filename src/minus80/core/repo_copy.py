"""Copy the head of one Icechunk repo's branch into another repo.

Icechunk has no cross-repo copy, so the source snapshot is walked node by node:

- every node's ``zarr.json`` is copied byte for byte;
- virtual chunks are declared again, at the same ranges of the same files;
- native and inline chunks are copied as stored, still encoded.

History is not copied. The copy is divided into units, the nodes at a fixed
depth below the root. A unit already in the target is skipped, so a stopped run
resumes, and the units under one group are committed together: each commit
costs a round trip per array it writes plus a new snapshot, which dominates the
copy itself.

:func:`check` compares a copy against its source.
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time

import numpy as np
from icechunk import ChunkType
from zarr.core.buffer import default_buffer_prototype
from zarr.core.chunk_key_encodings import parse_chunk_key_encoding

from minus80.core.icechunk_io import commit
from minus80.core.progress import Progress

BUFFER = default_buffer_prototype()


def meta_key(path: str) -> str:
    return f"{path}/zarr.json" if path else "zarr.json"


def chunk_grid(meta: dict) -> tuple[int, ...]:
    """The number of chunks along each dimension of an array's metadata."""
    grid, shape = meta["chunk_grid"], meta["shape"]
    if grid["name"] == "regular":
        chunks = grid["configuration"]["chunk_shape"]
        return tuple(math.ceil(s / c) for s, c in zip(shape, chunks))
    if grid["name"] == "rectilinear":
        counts = []
        for size, spec in zip(shape, grid["configuration"]["chunk_shapes"]):
            if isinstance(spec, int):
                counts.append(math.ceil(size / spec))
            else:
                # An entry is one edge, or a run-length pair [edge, repeats].
                counts.append(sum(e[1] if isinstance(e, list) else 1 for e in spec))
        return tuple(counts)
    raise ValueError(f"unsupported chunk grid {grid['name']!r}")


async def children(store, path: str) -> list[str]:
    """The child nodes of the group at ``path``."""
    prefix = f"{path}/" if path else ""
    names = [n async for n in store.list_dir(prefix)]
    return sorted(f"{prefix}{n.rstrip('/')}" for n in names if n != "zarr.json")


async def units_at(store, depth: int) -> tuple[list[str], list[str]]:
    """The groups above ``depth``, and the nodes at it."""
    parents, level = [], [""]
    for _ in range(depth):
        parents += level
        level = [c for p in level for c in await children(store, p)]
    return parents, level


class Copier:
    """Copies nodes from one store to another, counting what it moved."""

    def __init__(self, src, dst, concurrency: int):
        self.src, self.dst = src, dst
        self.limit = asyncio.Semaphore(concurrency)
        self.nodes = self.virtual = self.stored = self.stored_bytes = 0
        self.progress: Progress | None = None

    async def node(self, path: str) -> dict:
        raw = await self.src.get(meta_key(path), BUFFER)
        await self.dst.set(meta_key(path), raw)
        meta = json.loads(raw.to_bytes())
        if meta["node_type"] == "array":
            await self.chunks(path, meta)
        self.nodes += 1
        if self.progress:
            self.progress.advance()
        return meta

    async def tree(self, path: str) -> None:
        async with self.limit:
            meta = await self.node(path)
            below = await children(self.src, path) if meta["node_type"] == "group" else []
        await asyncio.gather(*(self.tree(child) for child in below))

    async def chunks(self, path: str, meta: dict) -> None:
        grid = chunk_grid(meta)
        n = math.prod(grid)
        locations = [""] * n
        offsets = np.zeros(n, dtype=np.uint64)
        lengths = np.zeros(n, dtype=np.uint64)
        stored: list[tuple[tuple[int, ...], bytes | None]] = []
        async for coords, kinds, paths, offs, lens, inlined in self.src.array_chunk_iterator(path):
            if not len(kinds):
                continue
            flat = np.ravel_multi_index(coords.T, grid) if grid else np.zeros(len(kinds), int)
            for i in np.flatnonzero(kinds == ChunkType.virtual):
                locations[flat[i]] = paths[i]
                offsets[flat[i]], lengths[flat[i]] = offs[i], lens[i]
            for i in np.flatnonzero(kinds != ChunkType.virtual):
                stored.append((tuple(int(x) for x in coords[i]), inlined.get(int(i))))
        if any(locations):
            failed = await self.dst.set_virtual_refs_arr_async(path, grid, locations, offsets, lengths)
            if failed:
                raise RuntimeError(f"{path}: {len(failed)} references outside the target's "
                                   f"virtual chunk containers, first {failed[0]}")
            self.virtual += sum(1 for loc in locations if loc)
        encoding = parse_chunk_key_encoding(meta["chunk_key_encoding"])

        async def copy(coords, data):
            key = f"{path}/{encoding.encode_chunk_key(coords)}"
            buffer = BUFFER.buffer.from_bytes(data) if data is not None else await self.src.get(key, BUFFER)
            if buffer is None:
                raise RuntimeError(f"{key} is listed in the manifest but has no bytes")
            async with self.limit:
                await self.dst.set(key, buffer)
            self.stored += 1
            self.stored_bytes += len(buffer)

        await asyncio.gather(*(copy(c, d) for c, d in stored))


async def copy_units(src, target, parents: list[str], units: list[str], origin: str,
                     concurrency: int = 64) -> None:
    """Copy ``parents`` and then each unit's subtree from ``src`` into ``target``'s main."""
    session = target.writable_session("main")
    copier = Copier(src, session.store, concurrency)
    for path in parents:
        await copier.node(path)
    if session.has_uncommitted_changes:
        await asyncio.to_thread(commit, session, f"Copy the groups above the copied units from {origin}")

    groups: dict[str, list[str]] = {}
    for unit in units:
        groups.setdefault(unit.rpartition("/")[0], []).append(unit)
    with Progress("subtrees", len(units), unit="subtrees", every=1) as overall:
        for group, members in groups.items():
            session = target.writable_session("main")
            copied = []
            for unit in members:
                if await session.store.exists(meta_key(unit)):
                    print(f"  {unit}: already in the target, skipped", flush=True)
                    overall.advance()
                    continue
                copier = Copier(src, session.store, concurrency)
                started = time.monotonic()
                with Progress(f"copying {unit}", unit="nodes", every=30) as copier.progress:
                    await copier.tree(unit)
                print(f"  {unit}: {copier.nodes:,} nodes, {copier.virtual:,} virtual refs, "
                      f"{copier.stored:,} stored chunks ({copier.stored_bytes / 2**20:,.1f} MiB) "
                      f"in {time.monotonic() - started:.1f} s", flush=True)
                copied.append(unit.rpartition("/")[2])
                overall.advance()
            if not copied:
                continue
            started = time.monotonic()
            where = f" in {group}" if group else ""
            snap = await asyncio.to_thread(
                commit, session, f"Copy {', '.join(copied)}{where} from {origin}")
            print(f"  {group or '/'}: committed {len(copied)} subtrees as {snap} "
                  f"in {time.monotonic() - started:.1f} s", flush=True)


async def walk(store, path: str, found: list[str], arrays: dict[str, dict]) -> None:
    found.append(path)
    meta = json.loads((await store.get(meta_key(path), BUFFER)).to_bytes())
    if meta["node_type"] == "array":
        arrays[path] = meta
        return
    for child in await children(store, path):
        await walk(store, child, found, arrays)


async def chunk_table(store, path: str) -> dict:
    table = {}
    async for coords, kinds, paths, offs, lens, inlined in store.array_chunk_iterator(path):
        for i in range(len(kinds)):
            table[tuple(int(x) for x in coords[i])] = (int(kinds[i]), paths[i], int(offs[i]),
                                                     int(lens[i]), inlined.get(i))
    return table


async def check(src, dst, unit: str, sample: int) -> dict[str, dict]:
    """Compare the copy of ``unit`` with its source, and return the unit's arrays.

    Every node's ``zarr.json`` must match byte for byte. On a random sample of
    arrays every chunk must match: the same location, offset and length for a
    virtual chunk, the same stored bytes for a native or inline one.
    """
    nodes: list[str] = []
    arrays: dict[str, dict] = {}
    await walk(src, unit, nodes, arrays)
    for path in nodes:
        a = await src.get(meta_key(path), BUFFER)
        b = await dst.get(meta_key(path), BUFFER)
        if b is None or a.to_bytes() != b.to_bytes():
            raise AssertionError(f"{meta_key(path)} differs")
    picked = random.Random(0).sample(sorted(arrays), min(sample, len(arrays)))
    native = virtual = 0
    for path in picked:
        encoding = parse_chunk_key_encoding(arrays[path]["chunk_key_encoding"])
        a, b = await chunk_table(src, path), await chunk_table(dst, path)
        if a.keys() != b.keys():
            raise AssertionError(f"{path}: chunk grids differ")
        for coords, (kind, loc, off, length, data) in a.items():
            if kind == ChunkType.virtual:
                if b[coords][1:4] != (loc, off, length):
                    raise AssertionError(f"{path} {coords}: reference differs")
                virtual += 1
            else:
                key = f"{path}/{encoding.encode_chunk_key(coords)}"
                x, y = await src.get(key, BUFFER), await dst.get(key, BUFFER)
                if x.to_bytes() != y.to_bytes():
                    raise AssertionError(f"{key}: bytes differ")
                native += 1
    print(f"  {unit}: {len(nodes):,} zarr.json identical; {len(picked)} arrays sampled, "
          f"{virtual:,} references and {native:,} stored chunks identical", flush=True)
    return arrays
