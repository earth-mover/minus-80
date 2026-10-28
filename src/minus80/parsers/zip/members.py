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

import struct
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
from virtualizarr.manifests import ManifestArray
from virtualizarr.parsers.zarr.zip import ZipEntry, _parse_zip64_extra, parse_zip_index

from minus80.core.aio import run_sync
from minus80.core.fetch import iter_ranges, object_size, read_range
from minus80.core.manifest import manifest_array
from minus80.core.registry import store_and_key

STORED = 0
DEFLATE = 8

#: A negative window size tells zlib the stream has no header or trailer.
RAW_WBITS = -15


#: How much of the archive's end to read for its end-of-central-directory
#: records: enough for the end record, a maximal comment and the ZIP64 records.
TAIL = 128 << 10

#: Pieces the central directory is fetched in, and how many at once.
DIRECTORY_PIECE = 8 << 20
DIRECTORY_CONCURRENCY = 8


def zip_index(url: str) -> dict[str, ZipEntry]:
    """Every file member of the archive at ``url``, by name, in central directory order.

    Reads only the archive's tail, its central directory, and at worst each
    member's 30-byte local header; no member data. The parsing is
    VirtualiZarr's ``parse_zip_index``.

    Why the central directory is fetched first
    -------------------------------------------
    ``parse_zip_index`` reads the whole central directory in one request.
    For a large archive that request is slow and fragile: S-BIAD3228's
    directory is 178 MB, and one request for it from EBI timed out from a
    laptop and ran for minutes from AWS London with nothing to show. Here the
    directory is fetched first in pieces, several at once, with progress
    lines, and the parser reads it from memory. Any other request the parser
    makes, such as the members' local headers, goes to the archive as usual.
    """
    from .buffers import end_of_central_directory

    store, key = store_and_key(url, patient=True)
    size = object_size(url, patient=True)
    tail_start = max(0, size - TAIL)
    tail = read_range(url, tail_start, size, patient=True)
    record = end_of_central_directory(tail, size)
    start, end = record.offset, record.offset + record.size
    directory = bytearray()
    for _, data in iter_ranges(
        url, [(start, end)], block=DIRECTORY_PIECE, concurrency=DIRECTORY_CONCURRENCY, patient=True
    ):
        directory += data

    def local_headers(offsets):
        return [read_range(url, o, o + _LOCAL_HEADER_SIZE, patient=True) for o in offsets]

    if (index := index_with_data_descriptors(bytes(directory), start, local_headers)) is not None:
        return index
    spans = [(tail_start, tail), (start, bytes(directory))]
    return run_sync(parse_zip_index(_Prefetched(store, size, spans), key))


#: The sizes a data descriptor can take: CRC-32 and two sizes of 4 or 8 bytes
#: each, with or without the optional 4-byte signature.
DATA_DESCRIPTOR_SIZES = (12, 16, 20, 24)
DATA_DESCRIPTOR = 0x08
_DIRECTORY_ENTRY = struct.Struct("<IHHHHHHIIIHHHHHII")
_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_LOCAL_HEADER_SIZE = 30


def index_with_data_descriptors(
    directory: bytes, directory_offset: int, local_headers
) -> dict[str, ZipEntry] | None:
    """The members of an archive whose members carry data descriptors, or None for any other archive.

    ``local_headers`` takes a list of local-header offsets and returns each
    header's first 30 bytes; it is called once, for a small sample.

    Why this exists
    ---------------
    VirtualiZarr's ``parse_zip_index`` trusts the offsets the central
    directory implies only when the members sit back to back. A member written
    with a data descriptor (flag bit 3) is followed by 12 to 24 bytes that
    record its sizes, so the members are never back to back, and the parser
    falls back to reading every member's local header. S-BIAD3228 has 2,036,714
    such members: 2 million requests, hours of work.

    The directory alone cannot place the data, either. Its extra field can be
    longer than the local header's: past 4 GB, S-BIAD3228's directory entries
    carry a 21-byte extra field (with the 8-byte header offset) while their
    local headers carry 9 bytes.

    How the members are placed instead
    ----------------------------------
    - Between the end of one member's compressed data and the next member's
      local header lie that member's local extra field and its data
      descriptor. The size of that space is computed for every member, and it
      must be the same for all of them: 25 bytes in S-BIAD3228.
    - A sample of local headers, spread across the archive, gives the local
      extra field's length (9 bytes). It must be the same in every sample,
      and the space minus it must be a data descriptor's size (16 bytes).
    - Each member's data then starts at its header offset plus 30 bytes, its
      name and that extra field.

    Anything else returns None, and the caller uses VirtualiZarr's parser.
    A member placed wrongly would not inflate to its recorded size, which
    :class:`~minus80.parsers.zip.store.MemberStore` checks on every read.
    docs/upstream.md tracks the fix in VirtualiZarr.
    """
    members, pos = [], 0
    while (
        pos + _DIRECTORY_ENTRY.size <= len(directory)
        and directory[pos : pos + 4] == _DIRECTORY_SIGNATURE
    ):
        fields = _DIRECTORY_ENTRY.unpack_from(directory, pos)
        flags, method, compressed, uncompressed = fields[3], fields[4], fields[8], fields[9]
        name_length, extra_length, comment_length = fields[10], fields[11], fields[12]
        header = fields[16]
        name = directory[pos + 46 : pos + 46 + name_length].decode()
        if 0xFFFFFFFF in (compressed, uncompressed, header):
            extra = directory[pos + 46 + name_length : pos + 46 + name_length + extra_length]
            uncompressed, compressed, header = _parse_zip64_extra(
                extra, uncompressed, compressed, header
            )
        pos += 46 + name_length + extra_length + comment_length
        if name.endswith("/") and uncompressed == 0:
            continue
        members.append((header, name, name_length, compressed, uncompressed, method, flags))
    if not members or not all(m[6] & DATA_DESCRIPTOR for m in members):
        return None
    members.sort()
    next_headers = [m[0] for m in members[1:]] + [directory_offset]
    spaces = {
        following - (header + _LOCAL_HEADER_SIZE + name_length + compressed)
        for (header, _, name_length, compressed, *_), following in zip(
            members, next_headers, strict=True
        )
    }
    if len(spaces) != 1:
        return None
    (space,) = spaces
    sample = members[:: max(1, len(members) // 8)] + [members[-1]]
    extras = set()
    for (_, name, name_length, *_), raw in zip(
        sample, local_headers([m[0] for m in sample]), strict=True
    ):
        if raw[:4] != _LOCAL_SIGNATURE or int.from_bytes(raw[26:28], "little") != name_length:
            return None
        extras.add(int.from_bytes(raw[28:30], "little"))
    if len(extras) != 1 or space - next(iter(extras)) not in DATA_DESCRIPTOR_SIZES:
        return None
    (extra,) = extras
    return {
        name: ZipEntry(
            header + _LOCAL_HEADER_SIZE + name_length + extra, compressed, uncompressed, method
        )
        for header, name, name_length, compressed, uncompressed, method, _ in members
    }


class _Prefetched:
    """A store that answers from bytes already fetched, and passes other requests on.

    ``spans`` are ``(start, bytes)`` pairs. A range wholly inside one is served
    from memory; anything else goes to ``store``.
    """

    def __init__(self, store, size: int, spans) -> None:
        self._store, self._size, self._spans = store, size, spans

    async def head_async(self, path):
        return {"size": self._size}

    async def get_range_async(self, path, *, start: int, end: int):
        for first, data in self._spans:
            if first <= start and end <= first + len(data):
                return data[start - first : end - first]
        return await self._store.get_range_async(path, start=start, end=end)

    async def get_ranges_async(self, path, *, starts, ends):
        return await self._store.get_ranges_async(path, starts=starts, ends=ends)


def cached_zip_index(url: str, path: Path) -> dict[str, ZipEntry]:
    """:func:`zip_index`, read from the archive once and kept in a file at ``path``.

    Why keep it
    -----------
    Finding a member in a zip means reading the archive's table of contents
    (its central directory) at the end of the file. For a large archive that is
    slow and worth doing only once: S-BIAD3228's is 178 MB for two million
    members and takes minutes to fetch from far away. A kept copy is also a
    record of exactly what the archive held when it was read.

    What the file holds
    -------------------
    A compressed ``.npz`` with one row per member (name, data offset,
    compressed and uncompressed length, compression method), plus the URL and
    the archive's size in bytes.

    When it is used
    ---------------
    - No file at ``path``: the index is read from the archive and written there.
    - A file for this ``url`` and an archive of the same size: the file is used,
      at the cost of one ``HEAD`` request to check the size.
    - A file for another URL, or an archive whose size has changed: raises
      ``ValueError`` rather than read members at stale offsets. Delete the file
      to read the archive again.
    """
    size = object_size(url)
    if path.exists():
        with np.load(path, allow_pickle=False) as saved:
            if str(saved["url"]) != url:
                raise ValueError(f"{path} indexes {saved['url']}, not {url}")
            if int(saved["size"]) != size:
                raise ValueError(
                    f"{path} indexes {url} at {int(saved['size'])} bytes; it is now {size}"
                )
            return {
                str(n): ZipEntry(int(o), int(c), int(u), int(m))
                for n, o, c, u, m in zip(
                    saved["names"],
                    saved["offsets"],
                    saved["compressed"],
                    saved["uncompressed"],
                    saved["methods"],
                )
            }
    index = zip_index(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = list(index.values())
    np.savez_compressed(
        path,
        url=url,
        size=size,
        names=np.array(list(index)),
        offsets=np.array([e.data_offset for e in entries], np.uint64),
        compressed=np.array([e.compressed_length for e in entries], np.uint64),
        uncompressed=np.array([e.uncompressed_length for e in entries], np.uint64),
        methods=np.array([e.method for e in entries], np.uint16),
    )
    return index


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


def inflate_prefix(
    url: str, entry: ZipEntry, size: int, *, block: int = 1 << 20
) -> tuple[bytes, int]:
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
        raise NotImplementedError(
            f"members use {sorted(methods)}; only STORED members can be chunks"
        )
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
        paths,
        offsets,
        lengths,
        shape=shape,
        chunk_shape=chunk_shape,
        dtype=dtype,
        codecs=member_codecs(entries, serializer),
        dimension_names=dimension_names,
        fill_value=fill_value,
        attributes=attributes,
    )
