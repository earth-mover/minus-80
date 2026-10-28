"""Parsing a zip's central directory and its members, from bytes already fetched.

Every function here parses bytes only: it takes bytes, or a table made from
them, and returns values, and fetches nothing. So the functions work the same
on bytes from a local file, an object store or an HTTP server, and tests hand
them bytes directly. Fetching is obstore's job, through
:mod:`minus80.core.fetch`; :mod:`minus80.parsers.zip.reading` fetches the bytes
and calls these functions.

What the central directory is
-----------------------------
A zip keeps its table of contents at the end of the file: one *entry* per
member, in the order the members are stored, then a few short end records that
say where the table starts. Each entry gives the member's name, its compressed
and uncompressed sizes, its compression method, a CRC of its uncompressed
bytes, and the offset of its *local header*: the short record just before the
member's data, which repeats the name and sizes.

Why local-header offsets, not data offsets
------------------------------------------
A virtual reference to a member must point at the first byte of its data, so a
virtual index has to know where every local header ends, which the directory
does not say. A copy does not need that: it fetches each member from its own
local header to the next member's, which holds the whole member, and reads the
local header from those bytes (:func:`parse_member`). So this module keeps the
offset the directory actually stores.

Members written by a streaming writer, such as every member of BioImage
Archive study S-BIAD3228, leave zeros in the local header's size fields and
write the sizes in a *data descriptor* after the data. :func:`parse_member`
therefore takes the sizes from the directory.

The parsing is the standard library's ``zipfile``, which reads only the
central directory when it opens an archive and resolves ZIP64 sizes and
offsets. It reads through :class:`~minus80.parsers.zip.buffers._TailFile`,
which presents the fetched bytes at their positions in the archive.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from collections.abc import Mapping

import numpy as np
import pyarrow as pa
from virtualizarr.parsers.zarr.zip import ZipEntry

from .buffers import _TailFile, _local_header, end_of_central_directory
from .members import member_bytes

#: How many of the archive's last bytes always hold the end records: the end
#: record, a comment of the largest allowed size, and the ZIP64 records.
END_RECORDS = 128 << 10
#: The fixed part of a central-directory entry, before its name, extra field
#: and comment.
_ENTRY_FIXED_BYTES = 46

#: One row per member; :func:`parse_central_directory` describes each column.
SCHEMA = pa.schema([
    ("name", pa.string()),
    ("local_header_offset", pa.uint64()),
    ("compressed", pa.uint64()),
    ("uncompressed", pa.uint64()),
    ("method", pa.uint16()),
    ("crc32", pa.uint32()),
    ("flags", pa.uint16()),
    ("version_made_by", pa.uint16()),
    ("version_needed", pa.uint16()),
    ("extra_ids", pa.string()),
    ("entry_offset", pa.uint64()),
    ("entry_bytes", pa.uint32()),
])


def parse_central_directory(archive_end: bytes, archive_size: int) -> pa.Table:
    """One row per member, in directory order, from the last bytes of an archive.

    Parsing, bytes only. ``archive_end`` must run to the end of the archive and
    start at or before the central directory. ``archive_size`` is the length
    of the whole archive, which places ``archive_end`` in it.

    Columns:

    - ``name``: the member's path in the archive.
    - ``local_header_offset``: where the member starts, ZIP64 resolved.
    - ``compressed``, ``uncompressed``, ``method``, ``crc32``, ``flags``: as
      the directory states them. Flag bit 3 means the member's sizes follow
      its data in a data descriptor.
    - ``version_made_by``: the writing system times 256 plus its zip version.
      ``version_needed``: the zip version a reader needs.
    - ``extra_ids``: the IDs of the entry's extra-field blocks, such as
      ``0x0001`` (ZIP64) or ``0x5455`` (a Unix timestamp).
    - ``entry_offset``, ``entry_bytes``: where the entry itself sits in the
      archive, and its length.

    The table's metadata holds ``archive_size``, ``directory_offset`` and
    ``directory_size``: where the last member ends is where the directory
    starts.

    Raises ``ValueError`` if the entries do not add up to the directory size
    the end record states, which would mean the directory holds something
    this parser did not read.
    """
    end_record = end_of_central_directory(archive_end[-END_RECORDS:], archive_size)
    archive = zipfile.ZipFile(_TailFile(archive_end, archive_size - len(archive_end)))
    entries = archive.infolist()
    entry_bytes = np.array(
        [_ENTRY_FIXED_BYTES + len(e.orig_filename.encode()) + len(e.extra) + len(e.comment) for e in entries],
        dtype=np.uint64,
    )
    if int(entry_bytes.sum()) != end_record.size:
        raise ValueError(
            f"the entries add up to {int(entry_bytes.sum())} bytes; the end record says {end_record.size}"
        )
    entry_offsets = end_record.offset + np.concatenate([[0], np.cumsum(entry_bytes)[:-1]]).astype(np.uint64)
    columns = {
        "name": [e.filename for e in entries],
        "local_header_offset": [e.header_offset for e in entries],
        "compressed": [e.compress_size for e in entries],
        "uncompressed": [e.file_size for e in entries],
        "method": [e.compress_type for e in entries],
        "crc32": [e.CRC for e in entries],
        "flags": [e.flag_bits for e in entries],
        "version_made_by": [e.create_system << 8 | e.create_version for e in entries],
        "version_needed": [e.extract_version for e in entries],
        "extra_ids": [extra_field_ids(e.extra) for e in entries],
        "entry_offset": entry_offsets,
        "entry_bytes": entry_bytes,
    }
    return pa.table(columns, schema=SCHEMA).replace_schema_metadata({
        "archive_size": str(archive_size),
        "directory_offset": str(end_record.offset),
        "directory_size": str(end_record.size),
    })


def extra_field_ids(extra: bytes) -> str:
    """The block IDs in an extra field, such as ``"0x0001 0x5455"``.

    Parsing, bytes only. An extra field is a run of blocks, each a 2-byte ID, a
    2-byte length and that many bytes of data.
    """
    ids, position = [], 0
    while position + 4 <= len(extra):
        block_id, length = struct.unpack_from("<HH", extra, position)
        ids.append(f"{block_id:#06x}")
        position += 4 + length
    return " ".join(ids)


def member_spans(directory: pa.Table) -> dict[str, tuple[int, int]]:
    """Each member's byte span, from its local header to the next member's local header.

    Parsing (of the table). The members of a zip sit back to back, so
    this span holds the member's local header, its data and any data
    descriptor, and fetching it needs no knowledge of how long the local
    header is. The last member ends where the central directory starts.
    """
    offsets = directory["local_header_offset"].to_numpy()
    order = np.argsort(offsets, kind="stable")
    starts = offsets[order]
    ends = np.append(starts[1:], int(directory.schema.metadata[b"directory_offset"]))
    names = directory["name"].to_numpy(zero_copy_only=False)[order]
    return {str(name): (int(start), int(end)) for name, start, end in zip(names, starts, ends)}


def parse_member(member: bytes, start: int, entry: Mapping) -> bytes:
    """A member's uncompressed bytes, from bytes that start at its local header.

    Parsing, bytes only. ``member`` starts at byte ``start`` of the archive, which
    must be the member's local header, and must hold the whole member, such as
    its span from :func:`member_spans`. ``entry`` is the member's row of
    :func:`parse_central_directory` (``name``, ``compressed``,
    ``uncompressed``, ``crc32``); its sizes are used because a member with a
    data descriptor has zeros in its local header.

    Raises ``ValueError`` if ``member`` does not start with this member's
    local header, if fewer bytes than the directory states were passed, or if
    the uncompressed bytes fail the directory's CRC.
    """
    local = _local_header(member, 0, start)
    if local is None or local.name != entry["name"]:
        raise ValueError(f"no local header for {entry['name']} at byte {start}")
    stored = ZipEntry(local.entry.data_offset, int(entry["compressed"]), int(entry["uncompressed"]), local.entry.method)
    first = stored.data_offset - start
    stored_bytes = member[first:first + stored.compressed_length]
    if len(stored_bytes) != stored.compressed_length:
        raise ValueError(f"{entry['name']}: {len(stored_bytes)} of {stored.compressed_length} stored bytes passed")
    uncompressed = member_bytes(stored_bytes, stored)
    if zlib.crc32(uncompressed) != int(entry["crc32"]):
        raise ValueError(f"{entry['name']}: the CRC does not match the directory's")
    return uncompressed
