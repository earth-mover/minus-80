"""The byte structures of a zip archive, parsed from bytes the caller already has.

Every function here takes bytes and returns values. The module imports only
the standard library, so it fetches nothing and loads no object store or
VirtualiZarr; ``tests/test_imports.py`` checks that. Each structure is parsed
here and nowhere else in ``minus80.parsers.zip``:

- the **end records** at the end of the archive, which say where the central
  directory is (:func:`end_of_central_directory`, reading through
  :class:`ArchiveTail`);
- a member's **local header**, the record just before its data
  (:func:`parse_local_header`), including its name, whose encoding depends on
  a flag (:func:`name_encoding`), and ZIP64 sizes (:func:`zip64_values`);
- a member's **stored bytes**, inflated when they are Deflate-compressed
  (:func:`inflate`).

A zip's layout, from the start of the file::

    │ local header │ name │ extra field │ data │ data descriptor? │ next local header │ ...
    │ central directory: one entry per member │ ZIP64 end record │ ZIP64 locator │ end record │

A writer that streams its output sets flag bit 3, leaves the local header's
sizes as zeros, and writes them in a data descriptor after the data.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from typing import NamedTuple

STORED = 0
DEFLATE = 8
#: A negative window size tells zlib the stream has no header or trailer, as
#: in a zip member.
RAW_WBITS = -15
#: How many of the archive's last bytes always hold the end records: the end
#: record, a comment of the largest allowed size, and the ZIP64 records.
END_RECORDS = 128 << 10

LOCAL_SIGNATURE = b"PK\x03\x04"
LOCAL_HEADER = struct.Struct("<4sHHHHHIIIHH")
#: Flag bit 3: the sizes follow the data in a data descriptor.
DATA_DESCRIPTOR_FLAG = 0x08
#: Flag bit 11: the name is UTF-8; without it, the name is code page 437.
UTF8_FLAG = 0x800
#: A 32-bit size or offset of all ones means: see the ZIP64 extra block.
ZIP64_SENTINEL = 0xFFFFFFFF
ZIP64_BLOCK = 0x0001


class EndRecord(NamedTuple):
    """Where an archive's central directory is, from its end records."""

    entries: int
    #: The central directory's length in bytes.
    size: int
    #: The central directory's offset from the start of the archive.
    offset: int


class LocalHeader(NamedTuple):
    """A member's local header, read from bytes."""

    name: str
    #: The general-purpose flags. With :data:`DATA_DESCRIPTOR_FLAG` set, the
    #: sizes below are zeros and the real ones follow the data.
    flags: int
    method: int
    compressed: int
    uncompressed: int
    #: Where the member's data starts, counted from the start of the archive.
    data_offset: int


class ArchiveTail(io.BytesIO):
    """The last bytes of an archive, seekable at their positions in the whole archive.

    ``zipfile`` reads end records and central directories from a file object.
    It checks the ZIP64 locator's offset against the position it read the
    locator from, so the bytes must appear at their real positions, not at 0.
    """

    def __init__(self, data: bytes, start: int) -> None:
        super().__init__(data)
        self._start = start

    def seek(self, position: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position -= self._start
        return super().seek(position, whence) + self._start

    def tell(self) -> int:
        return super().tell() + self._start


def end_of_central_directory(tail: bytes, archive_size: int) -> EndRecord:
    """The central directory's entry count, size and offset, from the last bytes of an archive.

    ``tail`` must end at the end of the archive and hold the end records:
    :data:`END_RECORDS` bytes always do. ``archive_size`` is the length of the
    whole archive. The parsing is ``zipfile``'s, ZIP64 included.

    Raises ``zipfile.BadZipFile`` if there is no end record.
    """
    record = zipfile._EndRecData(ArchiveTail(tail, archive_size - len(tail)))
    if not record:
        raise zipfile.BadZipFile("no end-of-central-directory record in the buffer")
    return EndRecord(record[zipfile._ECD_ENTRIES_TOTAL], record[zipfile._ECD_SIZE], record[zipfile._ECD_OFFSET])


def name_encoding(flags: int) -> str:
    """The encoding of a member's stored name: UTF-8 with flag bit 11, else code page 437."""
    return "utf-8" if flags & UTF8_FLAG else "cp437"


def zip64_values(extra: bytes, uncompressed: int, compressed: int, header_offset: int = 0) -> tuple[int, int, int]:
    """Replace each 32-bit value that is :data:`ZIP64_SENTINEL` with its 64-bit value from the extra field.

    The ZIP64 block holds 8-byte values only for the fields that overflowed,
    in the order uncompressed size, compressed size, local-header offset.

    Raises ``ValueError`` if a value overflowed but the block does not hold it.
    """
    position = 0
    while position + 4 <= len(extra):
        block_id, length = struct.unpack_from("<HH", extra, position)
        if block_id == ZIP64_BLOCK:
            values = extra[position + 4:position + 4 + length]
            replaced, read = [], 0
            for value in (uncompressed, compressed, header_offset):
                if value == ZIP64_SENTINEL:
                    if read + 8 > len(values):
                        raise ValueError("the ZIP64 extra block is shorter than its overflowed fields")
                    value = struct.unpack_from("<Q", values, read)[0]
                    read += 8
                replaced.append(value)
            return replaced[0], replaced[1], replaced[2]
        position += 4 + length
    if ZIP64_SENTINEL in (uncompressed, compressed, header_offset):
        raise ValueError("a field overflowed but the extra field has no ZIP64 block")
    return uncompressed, compressed, header_offset


def parse_local_header(buffer: bytes, position: int = 0, offset: int = 0) -> LocalHeader | None:
    """The local header that starts at ``buffer[position]``, or None if one cannot be read there.

    ``offset`` is ``buffer[0]``'s position in the archive, so the returned
    ``data_offset`` counts from the start of the archive. None when the bytes
    there are not a local header, the header runs past the end of
    ``buffer``, the name does not decode, or a ZIP64 size cannot be read. The
    member's data need not be in ``buffer``.
    """
    if position + LOCAL_HEADER.size > len(buffer):
        return None
    (signature, _, flags, method, _, _, _, compressed, uncompressed,
     name_length, extra_length) = LOCAL_HEADER.unpack_from(buffer, position)
    name_start = position + LOCAL_HEADER.size
    data_start = name_start + name_length + extra_length
    if signature != LOCAL_SIGNATURE or data_start > len(buffer):
        return None
    try:
        name = buffer[name_start:name_start + name_length].decode(name_encoding(flags))
        uncompressed, compressed, _ = zip64_values(buffer[name_start + name_length:data_start], uncompressed, compressed)
    except (UnicodeDecodeError, ValueError, struct.error):
        return None
    return LocalHeader(name, flags, method, compressed, uncompressed, offset + data_start)


def inflate(stored: bytes, method: int) -> bytes:
    """A member's contents from its stored bytes: as they are for STORED, inflated for Deflate.

    Raises ``NotImplementedError`` for any other method.
    """
    if method == STORED:
        return stored
    if method == DEFLATE:
        return zlib.decompress(stored, RAW_WBITS)
    raise NotImplementedError(f"zip compression method {method}")
