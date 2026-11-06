"""Zip archive members as the chunks of a VirtualiZarr array.

The archive is parsed by VirtualiZarr's ``parse_zip_index``, which reads the
end-of-central-directory record (and ZIP64's), the central directory, and the
local headers when their lengths cannot be trusted from the directory alone.
What is left is to place one member per chunk and name the codecs that decode
it.

One member is one chunk because a Deflate stream can only be decoded from its
start: the bytes inside a member, a TIFF's strips say, are not addressable on
their own.

Every ``source`` is a URL, a local path, or an open binary file.
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Mapping, Sequence
from zipfile import ZIP_DEFLATED, ZIP_STORED

import numpy as np
from virtualizarr.manifests import ManifestArray
from virtualizarr.parsers.zarr.zip import ZipEntry, parse_zip_index

from minus80.core.aio import run_sync
from minus80.core.manifest import manifest_array
from minus80.core.registry import as_url, store_and_key

from . import _source
from ._source import Source

#: A negative window size tells zlib the stream has no header or trailer.
RAW_WBITS = -zlib.MAX_WBITS


def zip_index(source: Source) -> dict[str, ZipEntry]:
    """Every file member of the archive, by name, in central directory order.

    Reads only the archive's tail, its central directory, and at worst each
    member's 30-byte local header; no member data.
    """
    if _source.is_file(source):
        store, key = _source.FileStore(source), ""
    else:
        store, key = store_and_key(os.fspath(source), patient=True)
    return run_sync(parse_zip_index(store, key))


def read_member(source: Source, entry: ZipEntry) -> bytes:
    """One member's uncompressed bytes, fetched by a single range request."""
    data = _source.read(source, entry.data_offset, entry.data_offset + entry.compressed_length)
    return member_bytes(data, entry)


def member_bytes(data: bytes, entry: ZipEntry) -> bytes:
    """A member's uncompressed bytes from its stored bytes, the ``entry.compressed_length`` bytes at ``entry.data_offset``."""
    if entry.method == ZIP_STORED:
        return data
    if entry.method == ZIP_DEFLATED:
        return zlib.decompress(data, RAW_WBITS)
    raise NotImplementedError(f"zip method {entry.method_name}")


def inflate_prefix(source: Source, entry: ZipEntry, size: int, *, block: int = 1 << 20) -> tuple[bytes, int]:
    """At least the first ``size`` uncompressed bytes of a Deflate member, and the compressed bytes it took.

    Reads the member ``block`` bytes at a time and inflates as it goes,
    stopping as soon as ``size`` bytes are out, so reaching byte n of a member
    costs about the compressed bytes before n and no more.
    """
    if entry.method != ZIP_DEFLATED:
        raise NotImplementedError(f"zip method {entry.method_name}")
    inflater = zlib.decompressobj(RAW_WBITS)
    out, pulled = bytearray(), 0
    span = (entry.data_offset, entry.data_offset + entry.compressed_length)
    pieces = _source.ranges(source, [span], block=block, concurrency=1, progress=False)
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
    methods = {e.method_name for e in entries if e.method != ZIP_STORED}
    if methods:
        raise NotImplementedError(f"members use {sorted(methods)}; only STORED members can be chunks")
    return [dict(serializer)]


def member_array(
    source: Source,
    members: np.ndarray,
    *,
    url: str | None = None,
    index: Mapping[str, ZipEntry] | None = None,
    shape: Sequence[int],
    chunk_shape: Sequence[int],
    dtype: np.dtype | str,
    serializer: Mapping,
    dimension_names: Sequence[str | None] | None = None,
    fill_value=0,
    attributes: Mapping | None = None,
) -> ManifestArray:
    """An array whose chunk at each grid position is the named member of the archive.

    ``members`` is shaped like the chunk grid and holds a member name, or
    ``None`` where the source has no file. That, or a zero-length member, is a
    missing chunk, read as ``fill_value``. ``serializer`` is the array-to-bytes
    codec that decodes one member's uncompressed bytes into a chunk, such as
    ``imagecodecs_tiff``.

    ``url`` is the address the manifest records, by default ``source`` itself;
    an open file has no address, so it must be given then. ``index`` is
    :func:`zip_index` of ``source``, read here when not passed.
    """
    if url is None:
        if _source.is_file(source):
            raise ValueError("url is required when source is an open file")
        url = as_url(source)
    if index is None:
        index = zip_index(source)
    members = np.asarray(members, dtype=object)
    unknown = [m for m in members.flat if m is not None and m not in index]
    if unknown:
        raise KeyError(f"not in {url}: {unknown[:3]}")
    paths = np.full(members.shape, None, dtype=object)
    offsets = np.zeros(members.shape, dtype=np.uint64)
    lengths = np.zeros(members.shape, dtype=np.uint64)
    for position, name in np.ndenumerate(members):
        if name is not None:
            paths[position] = url
            offsets[position] = index[name].data_offset
            lengths[position] = index[name].compressed_length
    entries = [index[m] for m in members.flat if m is not None]
    return manifest_array(
        paths, offsets, lengths, shape=shape, chunk_shape=chunk_shape, dtype=dtype,
        codecs=member_codecs(entries, serializer), dimension_names=dimension_names,
        fill_value=fill_value, attributes=attributes)
