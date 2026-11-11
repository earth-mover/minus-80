"""Zip members read from a window of bytes, without a central directory.

``virtualizarr.parsers.zarr.zip.parse_zip_index`` indexes an archive by reading
its whole central directory. That is the right tool until the directory is too
large to fetch (1.49 GB for 7.7M members), or when only a sample of members is
wanted. This module answers the same questions from a window of the archive that
the caller has already read:

- :func:`walk_local_headers`: the members of a window that starts at a local
  header, each located by the one before it.
- :func:`scan_local_headers`: the members whose local headers appear in a window
  that starts mid-archive.
- :func:`read_member`: one member's uncompressed bytes from a window.

:func:`walk_archive` applies :func:`walk_local_headers` to a whole remote
archive, fetching it in order in pieces (:func:`fetch_pieces`), for when every
member is wanted but the central directory is not.

The byte structures themselves (local headers, end records, inflating) are
parsed in :mod:`minus80.parsers.zip.structures`. Members are returned as
``virtualizarr`` ``ZipEntry`` records, with ``data_offset`` counted from the
start of the archive, so they can be used for virtual references directly. A
buffer's own position in the archive is passed as ``offset``.
"""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import obstore
from virtualizarr.parsers.zarr.zip import ZipEntry

from .structures import DATA_DESCRIPTOR_FLAG, LOCAL_HEADER, LOCAL_SIGNATURE, inflate, parse_local_header


class LocalMember(NamedTuple):
    """A member found through its local file header."""

    name: str
    #: ``data_offset`` is counted from the start of the archive.
    entry: ZipEntry
    #: The general-purpose bit flags of the local header.
    flags: int


def walk_local_headers(buf: bytes, offset: int = 0) -> Iterator[LocalMember]:
    """The members of ``buf``, which starts at a local header, in archive order.

    Each member is located by the one before it, so ``buf`` must begin at a member
    boundary (the first bytes of the archive, for instance). ``offset`` is the
    position of ``buf[0]`` in the archive. Stops at the first member whose data
    runs past the end of ``buf``, at the first byte that is not a local header
    (the central directory, say), or at a name that is not UTF-8.

    Raises ``ValueError`` for a member whose sizes are in a data descriptor after
    its data, since the walk cannot tell where such a member ends.
    """
    i = 0
    while i + LOCAL_HEADER.size < len(buf):
        if buf[i:i + 4] != LOCAL_SIGNATURE:
            return
        member = _local_member(buf, i, offset)
        if member is None:
            return
        if member.flags & DATA_DESCRIPTOR_FLAG:
            raise ValueError(f"{member.name} has its sizes in a data descriptor, so the walk cannot step over it")
        yield member
        i = member.entry.data_offset + member.entry.compressed_length - offset


def scan_local_headers(buf: bytes, offset: int = 0) -> Iterator[LocalMember]:
    """Every local header whose signature appears in ``buf``, wherever it falls.

    For a ``buf`` that starts mid-archive, where there is no member boundary to
    walk from. The signature can also occur inside compressed data, so a caller
    must check the names it gets. Members whose data does not fit in ``buf`` are
    skipped.
    """
    for found in re.finditer(re.escape(LOCAL_SIGNATURE), buf):
        i = found.start()
        if i + LOCAL_HEADER.size > len(buf):
            return
        member = _local_member(buf, i, offset)
        if member is not None:
            yield member


def read_member(buf: bytes, member: LocalMember, offset: int = 0) -> bytes:
    """The member's uncompressed bytes, from ``buf`` (which starts at ``offset`` in the archive).

    Handles STORED and DEFLATE members.
    """
    start = member.entry.data_offset - offset
    return inflate(buf[start:start + member.entry.compressed_length], member.entry.method)


class WalkedMember(NamedTuple):
    """A member met by :func:`walk_archive`."""

    #: Offset of the member's local header from the start of the archive.
    header_offset: int
    member: LocalMember
    #: The member's stored bytes: compressed, unless the member is STORED.
    data: memoryview


def fetch_pieces(store, key: str, pieces: Iterable[tuple[int, int]], *, threads: int = 32,
                 prefetch: int = 48) -> Iterator[bytes]:
    """The bytes of each ``(start, length)`` piece of ``key``, in order, with ``prefetch`` GETs in flight.

    ``store`` is an obstore store. Each piece is its own GET.
    ``obstore.get_ranges`` would coalesce neighbouring pieces, and for a whole
    archive that is one enormous GET held in memory.
    """
    with ThreadPoolExecutor(threads) as ex:
        pending, it = deque(), iter(pieces)
        for p in it:
            pending.append(ex.submit(_get, store, key, *p))
            if len(pending) == prefetch:
                break
        while pending:
            data = pending.popleft().result()
            if (p := next(it, None)) is not None:
                pending.append(ex.submit(_get, store, key, *p))
            yield data


def walk_archive(store, key: str, end: int, *, piece: int = 16 << 20, threads: int = 32,
                 prefetch: int = 48, max_carry: int = 256 << 20) -> Iterator[WalkedMember]:
    """Every member of the archive at ``key`` that lies before ``end``, in archive order.

    Reads ``[0, end)`` in GETs of ``piece`` bytes (see :func:`fetch_pieces`) and
    locates each local header where the member before it ends, so data offsets
    are exact without the central directory. Pass the central directory's
    offset (:func:`~minus80.parsers.zip.structures.end_of_central_directory`) as ``end`` to walk the whole
    archive; ending exactly there shows the members tile the data region.

    A member that spans pieces is carried into the next one. Raises
    ``ValueError`` when the carried bytes exceed ``max_carry`` (bytes that are
    not a local header, or a member larger than that), or when the walk stops
    short of ``end``. Members whose sizes are in a data descriptor raise too;
    see :func:`walk_local_headers`.
    """
    pieces = [(s, min(piece, end - s)) for s in range(0, end, piece)]
    buf, buf_offset = b"", 0
    for data in fetch_pieces(store, key, pieces, threads=threads, prefetch=prefetch):
        buf += data
        view = memoryview(buf)
        header_offset = buf_offset
        for m in walk_local_headers(buf, buf_offset):
            start, length = m.entry.data_offset, m.entry.compressed_length
            yield WalkedMember(header_offset, m, view[start - buf_offset:start - buf_offset + length])
            header_offset = start + length
        buf, buf_offset = buf[header_offset - buf_offset:], header_offset
        if len(buf) > max_carry:
            raise ValueError(f"walk stopped at offset {buf_offset}: not a local header")
    if buf_offset != end:
        raise ValueError(f"walk stopped at offset {buf_offset}, short of {end}")


def _get(store, key: str, start: int, length: int) -> bytes:
    return bytes(obstore.get_range(store, key, start=start, length=length))


def _local_member(buf: bytes, position: int, offset: int) -> LocalMember | None:
    """The member whose local header starts at ``buf[position]``, or None if its header or data is not all in ``buf``."""
    header = parse_local_header(buf, position, offset)
    if header is None or header.data_offset - offset + header.compressed > len(buf):
        return None
    entry = ZipEntry(header.data_offset, header.compressed, header.uncompressed, header.method)
    return LocalMember(header.name, entry, header.flags)
