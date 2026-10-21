"""Pull a remote file once (parallel range GETs) so a parser can index it locally.

Many parsers walk metadata scattered through a file (HDF5 chunk B-tree, TIFF IFD
chain, MRC extended header); doing that walk over HTTP is thousands of
latency-bound round trips. Pulling the whole file once -- in concurrent
block-sized range GETs to saturate bandwidth -- then parsing locally is far faster
and kinder to the server. The built repo still *references* the original remote
URL; this scratch copy is only for indexing.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence

from .progress import Progress
from .registry import store_and_key

#: ``virtual_oct.parser`` imports this name; the rule lives in
#: :func:`minus80.core.registry.store_and_key`, which is the single place a URL
#: becomes an object-store key.
_store_and_key = store_and_key


def fetch_head(url: str, nbytes: int) -> bytes:
    """Fetch just the first ``nbytes`` of a remote ``url`` via one range GET.

    For header-only indexing: a flat/self-describing format (MRC, and many others)
    keeps its layout in a small leading header, so a parser can compute every chunk's
    byte range from the first ~kilobytes without ever downloading the (potentially
    multi-GB) body. The built repo still references the full remote URL.
    """
    import obstore

    store, key = store_and_key(url)
    buf = obstore.get_range(store, key, start=0, end=nbytes)
    return bytes(memoryview(buf))


def download_then_index(
    url: str,
    dest_dir: str,
    *,
    block: int = 32 * 1024 * 1024,
    batch: int = 16,
) -> str:
    """Fetch ``url`` into ``dest_dir`` via parallel range GETs; return the path.

    Reuses a cached copy if already present. Use the returned local path only to
    hand to a parser library; keep referencing ``url`` in the manifest.
    """
    import obstore

    # Two sources can share a filename and hold different bytes: idr0095 names
    # 412 files with 19 distinct basenames, and `1X.nd2` alone names 54 of them.
    # Caching on the basename alone would hand the first file's pixels to all
    # 54, silently and without an error anywhere. So the containing directory
    # decides the cache slot, and the basename is kept inside it because
    # parsers sniff the extension.
    name = url.rsplit("/", 1)[-1]
    slot = hashlib.sha256(url[: len(url) - len(name)].encode()).hexdigest()[:16]
    # Absolute: a relative dest makes file://<rel> parse its first segment as a
    # host, which breaks registry prefix-stripping downstream.
    dest = os.path.abspath(os.path.join(dest_dir, slot, name))
    if os.path.exists(dest):
        return dest

    store, key = store_and_key(url)
    meta = obstore.head(store, key)
    size = meta["size"]

    starts = list(range(0, size, block))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Write to a temp file and atomically rename only once fully written, so an
    # interrupted download can't be mistaken for a complete cache hit next time
    # (f.truncate pre-sizes the file, so a partial write would otherwise look done).
    part = dest + ".part"
    with open(part, "wb") as f:
        f.truncate(size)
        for i in range(0, len(starts), batch):
            group = starts[i : i + batch]
            ends = [min(s + block, size) for s in group]
            results = obstore.get_ranges(store, key, starts=group, ends=ends)
            for start, end, buf in zip(group, ends, results):
                data = bytes(memoryview(buf))
                if len(data) != end - start:  # short read → would leave a zero gap
                    raise OSError(
                        f"short range read for {url} bytes [{start}, {end}): "
                        f"got {len(data)} of {end - start}"
                    )
                f.seek(start)
                f.write(data)
    os.replace(part, dest)
    return dest


def sparse_local(
    url: str,
    regions: Sequence[tuple[int, int]],
    dest_dir: str,
    *,
    block: int = 32 * 1024 * 1024,
    batch: int = 16,
) -> str:
    """Fetch only ``regions`` of ``url`` into a local file that is otherwise a hole.

    The file is created at the source's full length and the named byte ranges
    are written at their true offsets; everything else is a sparse hole that
    costs no disk. A parser handed the result sees a file of the right size with
    the parts it needs where it expects them, and reads them at local speed. One
    real case: 8 MiB written into a 156 GiB file, 8 MB on disk.

    **This is a workaround, and the shape of a better answer is known.** It
    exists because libraries like ``tifffile`` and ``h5py`` were built for local
    files and express their needs as seeks — they cannot be told "I will want
    these regions", so there is no way to hand them a fetch plan. Where the
    parser reads bytes directly rather than driving a file object, as
    ``async-tiff`` does, the right answer is to fetch the region and walk it in
    memory, and no local file is needed at all. See ``UPSTREAM.md``.

    What makes this worth having anyway is that it separates two decisions that
    a local-file parser fuses: *how to fetch* and *how to parse*. Fetch by
    whatever strategy the layout permits — one bulk read for a contiguous
    metadata region — then let the parser seek as freely as it likes against
    something local. Nothing here is specific to a format; the caller says which
    ranges matter, and locating them is that format's business.

    Caching is keyed on the source *and* the regions asked for, so a later call
    wanting different ranges does not silently get the earlier file.
    """
    import obstore

    name = url.rsplit("/", 1)[-1]
    wanted = sorted((int(a), int(b)) for a, b in regions)
    if not wanted:
        raise ValueError(f"no regions asked for from {url}")
    signature = repr((url[: len(url) - len(name)], wanted)).encode()
    slot = hashlib.sha256(signature).hexdigest()[:16]
    dest = os.path.abspath(os.path.join(dest_dir, slot, name))
    if os.path.exists(dest):
        return dest

    store, key = store_and_key(url)
    size = obstore.head(store, key)["size"]
    if wanted[-1][1] > size:
        raise ValueError(f"{url}: asked for bytes past the end of a {size}-byte file")

    # One request per block, not per region: a region may be hundreds of MB.
    reads = [(s, min(s + block, end))
             for start, end in wanted
             for s in range(start, end, block)]

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    asked = sum(b - a for a, b in reads)
    with open(part, "wb") as f, Progress(
            f"fetching {name}", asked, unit="MiB", scale=1 << 20) as note:
        f.truncate(size)                     # the holes; no disk until written
        for i in range(0, len(reads), batch):
            group = reads[i : i + batch]
            results = obstore.get_ranges(
                store, key, starts=[a for a, _ in group], ends=[b for _, b in group]
            )
            for (start, end), buf in zip(group, results):
                data = bytes(memoryview(buf))
                if len(data) != end - start:
                    raise OSError(
                        f"short range read for {url} bytes [{start}, {end}): "
                        f"got {len(data)} of {end - start}"
                    )
                f.seek(start)
                f.write(data)
                note.advance(end - start)
    os.replace(part, dest)
    return dest
