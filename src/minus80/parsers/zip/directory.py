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

The directory is parsed by the standard library's ``zipfile``, which reads
only the central directory when it opens an archive and resolves ZIP64 sizes
and offsets; local headers, end records and inflating are parsed in
:mod:`minus80.parsers.zip.structures`. Neither module imports anything that
fetches.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from collections.abc import Iterable, Mapping

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .structures import END_RECORDS, ArchiveTail, end_of_central_directory, inflate, name_encoding, parse_local_header
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
    archive = zipfile.ZipFile(ArchiveTail(archive_end, archive_size - len(archive_end)))
    entries = archive.infolist()
    # Names count in the encoding they are stored in, which zipfile undid.
    entry_bytes = np.array(
        [_ENTRY_FIXED_BYTES + len(e.orig_filename.encode(name_encoding(e.flag_bits))) + len(e.extra) + len(e.comment)
         for e in entries],
        dtype=np.uint64,
    )
    if int(entry_bytes.sum()) != end_record.size:
        raise ValueError(
            f"the entries add up to {int(entry_bytes.sum())} bytes; the end record says {end_record.size}"
        )
    entry_offsets = np.uint64(end_record.offset) + np.cumsum(entry_bytes, dtype=np.uint64) - entry_bytes
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


def member_spans(directory: pa.Table, names: Iterable[str] | None = None) -> pa.Table:
    """Each member's byte span in the archive, from its local header to the next member's local header.

    Parsing (of the table). The members of a zip sit back to back, so this
    span holds the member's local header, its data and any data descriptor,
    and fetching it needs no knowledge of how long the local header is. The
    last member ends where the central directory starts.

    Returns a table with ``entry`` (the member's row in ``directory``),
    ``name``, ``start`` and ``end``, sorted by ``start``. With ``names``, only
    those members, each found by a binary search among the sorted offsets;
    raises ``KeyError`` for a name the directory does not hold.
    """
    offsets = directory["local_header_offset"].to_numpy()
    order = np.argsort(offsets, kind="stable")
    sorted_offsets = offsets[order]
    directory_offset = np.uint64(int(directory.schema.metadata[b"directory_offset"]))
    if names is None:
        rows = order
    else:
        wanted = list(names)
        mask = pc.is_in(directory["name"], value_set=pa.array(wanted, pa.string()))
        found = np.flatnonzero(mask.to_numpy(zero_copy_only=False))
        missing = set(wanted) - set(directory["name"].take(pa.array(found, pa.int64())).to_pylist())
        if missing:
            raise KeyError(f"not in the archive: {sorted(missing)[:5]}")
        rows = found[np.argsort(offsets[found], kind="stable")]
    starts = offsets[rows]
    following = np.searchsorted(sorted_offsets, starts, side="right")
    padded = np.append(sorted_offsets, directory_offset)
    ends = padded[following]
    return pa.table({
        "entry": pa.array(rows, pa.int64()),
        "name": directory["name"].take(pa.array(rows, pa.int64())),
        "start": pa.array(starts, pa.uint64()),
        "end": pa.array(ends, pa.uint64()),
    })


#: How much of a span to read a local header from: its fixed 30 bytes plus a
#: name and extra field. A header that needs more is read from the whole span.
_HEADER_WINDOW = 4096


def member_stored_bytes(span: bytes | memoryview, start: int, entry: Mapping) -> bytes:
    """A member's bytes as the zip stores them, cut from bytes that start at its local header.

    Parsing, bytes only. ``span`` starts at byte ``start`` of the archive,
    which must be the member's local header, and must hold the whole member,
    such as its span from :func:`member_spans`. ``entry`` is the member's row
    of :func:`parse_central_directory` (``name`` and ``compressed``); the
    compressed size comes from it because a member with a data descriptor has
    zeros in its local header.

    Only the local header is read to find where the data starts, so the data
    is copied once, whatever ``span`` is.

    Raises ``ValueError`` if ``span`` does not start with this member's local
    header, or holds fewer bytes than the directory states.
    """
    local = parse_local_header(bytes(span[:_HEADER_WINDOW]), 0, start) or parse_local_header(bytes(span), 0, start)
    if local is None or local.name != entry["name"]:
        raise ValueError(f"no local header for {entry['name']} at byte {start}")
    compressed = int(entry["compressed"])
    first = local.data_offset - start
    stored = bytes(span[first:first + compressed])
    if len(stored) != compressed:
        raise ValueError(f"{entry['name']}: {len(stored)} of {compressed} stored bytes passed")
    return stored


def inflate_member(stored: bytes, entry: Mapping) -> bytes:
    """A member's contents from its stored bytes, checked against the directory's CRC.

    Parsing, bytes only. ``entry`` is the member's row of
    :func:`parse_central_directory` (``name``, ``method`` and ``crc32``).
    zlib releases the GIL while it inflates, so callers can inflate many
    members at once on a thread pool.

    Raises ``ValueError`` if the inflated bytes fail the CRC.
    """
    contents = inflate(stored, int(entry["method"]))
    if zlib.crc32(contents) != int(entry["crc32"]):
        raise ValueError(f"{entry['name']}: the CRC does not match the directory's")
    return contents


def parse_member(member: bytes, start: int, entry: Mapping) -> bytes:
    """A member's contents, from bytes that start at its local header.

    Parsing, bytes only: :func:`member_stored_bytes`, then
    :func:`inflate_member`. ``entry`` is the member's row of
    :func:`parse_central_directory`.
    """
    return inflate_member(member_stored_bytes(member, start, entry), entry)
