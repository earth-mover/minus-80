"""Local copies of remote files, for a parser to index.

A parser that walks metadata scattered through a file (an HDF5 chunk B-tree, a
TIFF directory chain, an MRC extended header) makes one latency-bound round trip
per structure when it reads over HTTP, and a large file has thousands of them.
These functions fetch the bytes the parser needs in concurrent block-sized range
GETs and write them to a local file, so the walk runs at disk speed. The manifest
still references the remote URL; the local copy is only read during indexing.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence

from .progress import Progress
from .registry import store_and_key

#: Parsers import this name from here. The rule itself lives in
#: :func:`minus80.core.registry.store_and_key`, the one place a URL becomes an
#: object-store key.
_store_and_key = store_and_key


def fetch_head(url: str, nbytes: int) -> bytes:
    """Fetch the first ``nbytes`` of a remote ``url`` with one range GET.

    For a format such as MRC that keeps its whole layout in a leading header,
    the first few kilobytes are enough to compute every chunk's byte range, and
    the body is never downloaded.
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
    """Fetch ``url`` into ``dest_dir`` with parallel range GETs and return the path.

    ``block`` is the size of each range GET and ``batch`` how many are issued at
    once. A copy already in ``dest_dir`` is reused. Hand the returned path to a
    parser and keep ``url`` in the manifest.
    """
    import obstore

    # Two sources can share a filename and hold different bytes: idr0095 names
    # 412 files with 19 distinct basenames, and `1X.nd2` alone names 54 of them.
    # A cache keyed on the basename would silently hand the first file's bytes
    # to all 54. The containing directory picks the cache slot, and the basename
    # is kept inside it because parsers sniff the extension.
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
    # f.truncate pre-sizes the file, so a partial download would have the right
    # size and pass for a cache hit. Writing to .part and renaming at the end
    # keeps an interrupted download out of the cache.
    part = dest + ".part"
    with open(part, "wb") as f:
        f.truncate(size)
        for i in range(0, len(starts), batch):
            group = starts[i : i + batch]
            ends = [min(s + block, size) for s in group]
            results = obstore.get_ranges(store, key, starts=group, ends=ends)
            for start, end, buf in zip(group, ends, results):
                data = bytes(memoryview(buf))
                # A short read would leave a run of zeros in the pre-sized file.
                if len(data) != end - start:
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
    takes no disk space. A parser handed the result sees a file of the right
    size with the parts it needs at the offsets it expects. Writing 8 MiB of a
    156 GiB file this way uses 8 MB of disk.

    Use it for a parser such as ``tifffile`` or ``h5py`` that drives a file
    object with seeks and cannot be given a list of regions to fetch. The caller
    decides which ranges matter, for instance one contiguous metadata region,
    and fetches them in bulk; the parser then seeks freely against a local
    file. A parser that reads bytes directly, as ``async-tiff`` does, can walk
    the fetched region in memory instead (docs/upstream.md).

    The cache is keyed on the source and the regions, so a later call asking for
    different ranges does not get the earlier file.
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

    # Split by block as well as by region: one region may be hundreds of MB.
    reads = [(s, min(s + block, end))
             for start, end in wanted
             for s in range(start, end, block)]

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    asked = sum(b - a for a, b in reads)
    with open(part, "wb") as f, Progress(
            f"fetching {name}", asked, unit="MiB", scale=1 << 20) as note:
        f.truncate(size)
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
