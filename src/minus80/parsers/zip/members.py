"""Zip archive members as the chunks of a VirtualiZarr array.

The archive is parsed by VirtualiZarr's ``parse_zip_index``, which reads the
end-of-central-directory record (and ZIP64's), the central directory, and the
local headers when their lengths cannot be trusted from the directory alone.
What is left is to place one member per chunk and name the codecs that decode
it, which is all this does.

One member is one chunk because a Deflate stream can only be decoded from its
start: the bytes inside a member, a TIFF's strips say, are not addressable on
their own.
"""

from __future__ import annotations

import zlib
from collections.abc import Mapping, Sequence

import numpy as np
from virtualizarr.manifests import ManifestArray
from virtualizarr.parsers.zarr.zip import ZipEntry, parse_zip_index

from minus80.core.aio import run_sync
from minus80.core.fetch import iter_ranges, read_range
from minus80.core.manifest import manifest_array
from minus80.core.registry import store_and_key

STORED = 0
DEFLATE = 8

#: A negative window size tells zlib the stream has no header or trailer.
RAW_WBITS = -15


def zip_index(url: str) -> dict[str, ZipEntry]:
    """Every file member of the archive at ``url``, by name, in central directory order.

    Reads only the archive's tail, its central directory, and at worst each
    member's 30-byte local header; no member data.
    """
    store, name = store_and_key(url)
    return run_sync(parse_zip_index(store, name))


def read_member(url: str, entry: ZipEntry) -> bytes:
    """One member's uncompressed bytes, fetched by a single range request."""
    data = read_range(url, entry.data_offset, entry.data_offset + entry.compressed_length)
    return member_bytes(data, entry)


def member_bytes(data: bytes, entry: ZipEntry) -> bytes:
    """A member's uncompressed bytes from its stored bytes, the ``entry.compressed_length`` bytes at ``entry.data_offset``."""
    if entry.method == STORED:
        return data
    if entry.method == DEFLATE:
        return zlib.decompress(data, RAW_WBITS)
    raise NotImplementedError(f"zip method {entry.method_name}")


def inflate_prefix(url: str, entry: ZipEntry, size: int, *, block: int = 1 << 20) -> tuple[bytes, int]:
    """At least the first ``size`` uncompressed bytes of a Deflate member, and the compressed bytes it took.

    Range-reads the member ``block`` bytes at a time and inflates as it goes,
    stopping as soon as ``size`` bytes are out, so reaching byte n of a member
    costs about the compressed bytes before n and no more.
    """
    if entry.method != DEFLATE:
        raise NotImplementedError(f"zip method {entry.method_name}")
    inflater = zlib.decompressobj(RAW_WBITS)
    out, pulled = bytearray(), 0
    span = (entry.data_offset, entry.data_offset + entry.compressed_length)
    pieces = iter_ranges(url, [span], block=block, concurrency=1, progress=False)
    for _, data in pieces:
        pulled += len(data)
        out += inflater.decompress(data)
        if len(out) >= size:
            break
    pieces.close()
    return bytes(out), pulled


def member_codecs(entries: Sequence[ZipEntry], serializer: Mapping) -> list[dict]:
    """The codec chain for chunks that are these members, which must all be STORED.

    Deflate members would need a raw-Deflate Zarr codec. imagecodecs-zarr's
    ``Deflate(raw=True)`` is one, but it cannot decode a chunk without being
    told its uncompressed size.
    """
    methods = {e.method_name for e in entries if e.method != STORED}
    if methods:
        raise NotImplementedError(f"members use {sorted(methods)}; only STORED members can be chunks")
    return [dict(serializer)]


def member_array(
    url: str,
    index: Mapping[str, ZipEntry],
    members: np.ndarray,
    *,
    shape: Sequence[int],
    chunk_shape: Sequence[int],
    dtype: np.dtype | str,
    serializer: Mapping,
    dimension_names: Sequence[str | None] | None = None,
    fill_value=0,
    attributes: Mapping | None = None,
) -> ManifestArray:
    """An array whose chunk at each grid position is the named member of the archive at ``url``.

    ``members`` is shaped like the chunk grid and holds a member name, or
    ``None`` where the source has no file. That, or a zero-length member, is a
    missing chunk, read as ``fill_value``. ``serializer`` is the array-to-bytes
    codec that decodes one member's uncompressed bytes into a chunk, such as
    ``imagecodecs_tiff``.
    """
    members = np.asarray(members, dtype=object)
    grid = tuple(-(-s // c) for s, c in zip(shape, chunk_shape))
    if members.shape != grid:
        raise ValueError(f"members have shape {members.shape}, the chunk grid is {grid}")
    present = [m for m in members.flat if m is not None]
    missing = [m for m in present if m not in index]
    if missing:
        raise KeyError(f"not in {url}: {missing[:3]}")
    entries = [index[m] for m in present]
    paths = np.full(grid, None, dtype=object)
    offsets = np.zeros(grid, dtype=np.uint64)
    lengths = np.zeros(grid, dtype=np.uint64)
    for position, name in np.ndenumerate(members):
        if name is not None:
            paths[position] = url
            offsets[position] = index[name].data_offset
            lengths[position] = index[name].compressed_length
    return manifest_array(
        paths, offsets, lengths, shape=shape, chunk_shape=chunk_shape, dtype=dtype,
        codecs=member_codecs(entries, serializer), dimension_names=dimension_names,
        fill_value=fill_value, attributes=attributes)
