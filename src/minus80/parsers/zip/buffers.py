"""Zip structures read from a buffer of bytes, without a central directory.

``virtualizarr.parsers.zarr.zip.parse_zip_index`` indexes an archive by reading
its whole central directory. That is the right tool until the directory is too
large to fetch (1.49 GB for 7.7M members), or when only a sample of members is
wanted. This module answers the same questions from a window of the archive that
the caller has already read:

- :func:`end_of_central_directory`: how many members, and where the central
  directory sits, from the archive's last bytes (ZIP64 included).
- :func:`walk_local_headers`: the members of a window that starts at a local
  header, each located by the one before it.
- :func:`scan_local_headers`: the members whose local headers appear in a window
  that starts mid-archive.
- :func:`member_from_buffer`: one member's uncompressed bytes from a window.

:func:`walk_archive` applies :func:`walk_local_headers` to a whole archive,
fetching it in order in pieces, for when every member is wanted but the
central directory is not.

Members are returned as ``virtualizarr`` ``ZipEntry`` records, with
``data_offset`` counted from the start of the archive, so they can be used for
virtual references directly. A buffer's own position in the archive is passed as
``offset``.

Two private functions are imported because nothing public does their job:
``zipfile._EndRecData`` (the standard library's end-record reader, which handles
ZIP64) and VirtualiZarr's ``_parse_zip64_extra`` (resolves the sizes of a ZIP64
local header).
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
from collections.abc import Iterator
from typing import NamedTuple

from virtualizarr.parsers.zarr.zip import ZipEntry, _parse_zip64_extra

from . import _source
from ._source import Source
from .members import member_bytes

_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_LOCAL_SIGNATURE = b"PK\x03\x04"
_ZIP64_SENTINEL = 0xFFFFFFFF
_DATA_DESCRIPTOR = 0x08
#: Enough of an archive's end to hold the end record behind the longest
#: comment, the ZIP64 locator and the ZIP64 end record.
_TAIL = (1 << 16) + 22 + 20 + 56


class EndRecord(NamedTuple):
    """The members of an archive and where its central directory starts."""

    entries: int
    #: Size of the central directory in bytes.
    size: int
    #: Offset of the central directory from the start of the archive.
    offset: int


class LocalMember(NamedTuple):
    """A member found through its local file header."""

    name: str
    #: ``data_offset`` is counted from the start of the archive.
    entry: ZipEntry
    #: The general-purpose bit flags of the local header.
    flags: int


def end_of_central_directory(tail: bytes, archive_size: int) -> EndRecord:
    """The central directory's member count, size and offset, from the last bytes of an archive.

    ``tail`` must end at the end of the archive and be long enough to hold the end
    record, the ZIP64 end record and its locator (64 KiB is ample unless the
    archive has a long comment). The directory itself is not needed.
    ``archive_size`` is the length of the whole archive.

    Raises ``zipfile.BadZipFile`` if no end record is found.
    """
    record = zipfile._EndRecData(_TailFile(tail, archive_size - len(tail)))
    if not record:
        raise zipfile.BadZipFile("no end-of-central-directory record in the buffer")
    return EndRecord(record[zipfile._ECD_ENTRIES_TOTAL], record[zipfile._ECD_SIZE], record[zipfile._ECD_OFFSET])


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
    while i + _LOCAL_HEADER.size < len(buf):
        if buf[i:i + 4] != _LOCAL_SIGNATURE:
            return
        member = _local_header(buf, i, offset)
        if member is None:
            return
        if member.flags & _DATA_DESCRIPTOR:
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
    for found in re.finditer(re.escape(_LOCAL_SIGNATURE), buf):
        i = found.start()
        if i + _LOCAL_HEADER.size > len(buf):
            return
        member = _local_header(buf, i, offset)
        if member is not None:
            yield member


def member_from_buffer(buf: bytes, member: LocalMember, offset: int = 0) -> bytes:
    """The member's uncompressed bytes, from ``buf`` (which starts at ``offset`` in the archive).

    Handles STORED and DEFLATE members.
    """
    start = member.entry.data_offset - offset
    return member_bytes(buf[start:start + member.entry.compressed_length], member.entry)


class WalkedMember(NamedTuple):
    """A member met by :func:`walk_archive`."""

    #: Offset of the member's local header from the start of the archive.
    header_offset: int
    member: LocalMember
    #: The member's stored bytes: compressed, unless the member is STORED.
    data: memoryview


def walk_archive(source: Source, end: int | None = None, *, piece: int = 16 << 20, concurrency: int = 32,
                 max_carry: int = 256 << 20) -> Iterator[WalkedMember]:
    """Every member of the archive that lies before ``end``, in archive order.

    Reads ``[0, end)`` in GETs of ``piece`` bytes, ``concurrency`` at a time,
    and locates each local header where the member before it ends, so data
    offsets are exact without the central directory. ``end`` defaults to the
    central directory's offset, read from the archive's tail with
    :func:`end_of_central_directory`; ending exactly there shows the members
    tile the data region.

    A member that spans pieces is carried into the next one. Raises
    ``ValueError`` when the carried bytes exceed ``max_carry`` (bytes that are
    not a local header, or a member larger than that), or when the walk stops
    short of ``end``. Members whose sizes are in a data descriptor raise too;
    see :func:`walk_local_headers`. A short read raises ``OSError``.
    """
    if end is None:
        size = _source.size(source, patient=True)
        tail_start = max(0, size - _TAIL)
        end = end_of_central_directory(_source.read(source, tail_start, size, patient=True), size).offset
    buf, buf_offset = b"", 0
    for _, data in _source.ranges(source, [(0, end)], block=piece, concurrency=concurrency,
                                  progress=True, patient=True):
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


class _TailFile(io.BytesIO):
    """The last bytes of an archive, seekable at their positions in the whole archive.

    ``zipfile._EndRecData`` checks the ZIP64 locator's absolute offset against
    the position it was read from, so a bare buffer of the tail fails that check.
    """

    def __init__(self, tail: bytes, start: int) -> None:
        super().__init__(tail)
        self._start = start

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos -= self._start
        return super().seek(pos, whence) + self._start

    def tell(self) -> int:
        return super().tell() + self._start


def _local_header(buf: bytes, i: int, offset: int) -> LocalMember | None:
    """The member whose local header starts at ``buf[i]``, or None if it cannot be read from ``buf``.

    None when the name is not UTF-8, the ZIP64 extra field is unreadable, or the
    data runs past the end of ``buf``.
    """
    (_, _, flags, method, _, _, _, csize, usize, nlen, xlen) = _LOCAL_HEADER.unpack_from(buf, i)
    try:
        name = buf[i + 30:i + 30 + nlen].decode()
    except UnicodeDecodeError:
        return None
    if _ZIP64_SENTINEL in (csize, usize):
        try:
            usize, csize, _ = _parse_zip64_extra(buf[i + 30 + nlen:i + 30 + nlen + xlen], usize, csize, 0)
        except struct.error:
            return None
    start = i + 30 + nlen + xlen
    if start + csize > len(buf):
        return None
    return LocalMember(name, ZipEntry(offset + start, csize, usize, method), flags)
