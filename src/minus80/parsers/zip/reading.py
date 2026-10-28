"""Reading a remote zip's central directory and members: fetch the bytes, then parse them.

Each function here does two steps and nothing else:

- **Fetching** (obstore, through :mod:`minus80.core.fetch`): ``object_size``,
  ``read_range`` and ``iter_ranges`` with the caller's ``ObjectStoreRegistry``.
  ``minus80.core.fetch`` is a thin layer over obstore that streams ranges in
  order with bounded concurrency and checks that every byte arrived.
- **Parsing** (bytes only): the functions of
  :mod:`minus80.parsers.zip.directory`, which never fetch.

Every function takes the archive's URL and an optional registry, the same
pair VirtualiZarr parsers receive, so the same code reads a local file, an
object store or an HTTP server, depending only on the registry.

These functions are for copying an archive, not for referencing it
virtually: they work from the offsets the central directory stores and read
each member's local header from bytes they have fetched (see
:mod:`minus80.parsers.zip.directory`).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import obstore
import pyarrow as pa
import pyarrow.parquet as pq
from obspec_utils.registry import ObjectStoreRegistry

from minus80.core.fetch import iter_ranges, read_range
from minus80.core.registry import resolve, store_and_key

from .directory import member_spans, parse_central_directory, parse_member
from .structures import END_RECORDS, end_of_central_directory

#: The central directory is fetched in pieces of this size, this many at once.
#: One request for S-BIAD3228's 178.7 MB directory ran for minutes from AWS
#: London with nothing to show; 8 MiB pieces, 8 at once, took about 10 s.
DIRECTORY_PIECE = 8 << 20
DIRECTORY_CONCURRENCY = 8
#: The fields of a ``HEAD`` that identify one version of the archive, most
#: specific first. Servers send some or all of them.
VERSION_FIELDS = ("e_tag", "last_modified", "size")


def archive_version(url: str, *, registry: ObjectStoreRegistry | None = None) -> dict[str, str]:
    """The archive's size and, where the server sends them, its ETag and last-modified time, from one ``HEAD``.

    ``minus80.core.fetch.object_size`` returns only the size, so this calls
    obstore's ``head`` directly, through the same store lookup.
    """
    store, key = resolve(registry, url) if registry is not None else store_and_key(url, patient=True)
    head = obstore.head(store, key)
    return {field: str(head[field]) for field in VERSION_FIELDS if head.get(field) is not None}


def read_central_directory(url: str, *, registry: ObjectStoreRegistry | None = None) -> pa.Table:
    """The central directory of the archive at ``url``, one row per member.

    Fetches the archive's last :data:`END_RECORDS` bytes to find the
    directory. If the directory starts before them, fetches the rest from
    the directory's start, in :data:`DIRECTORY_PIECE` pieces,
    :data:`DIRECTORY_CONCURRENCY` at a time. Parses it with
    :func:`~minus80.parsers.zip.directory.parse_central_directory`, and
    records the archive's version (:func:`archive_version`) in the table's
    metadata. No member is read.
    """
    version = archive_version(url, registry=registry)
    archive_size = int(version["size"])
    tail_start = max(0, archive_size - END_RECORDS)
    tail = read_range(url, tail_start, archive_size, patient=True, registry=registry)
    end_record = end_of_central_directory(tail, archive_size)
    if end_record.offset >= tail_start:
        archive_end = tail[end_record.offset - tail_start:]
    else:
        before_tail = bytearray()
        for _, piece in iter_ranges(
            url, [(end_record.offset, tail_start)], block=DIRECTORY_PIECE,
            concurrency=DIRECTORY_CONCURRENCY, patient=True, registry=registry,
        ):
            before_tail += piece
        archive_end = bytes(before_tail) + tail
    directory = parse_central_directory(archive_end, archive_size)
    recorded = {f"archive_{field}": value for field, value in version.items() if field != "size"}
    return directory.replace_schema_metadata({**directory.schema.metadata, **recorded})


def _same_version(recorded: dict[str, str], current: dict[str, str]) -> bool:
    """Whether a kept directory still describes the archive, by the most specific field both sides have."""
    for field in VERSION_FIELDS:
        key = "archive_size" if field == "size" else f"archive_{field}"
        if key in recorded and field in current:
            return recorded[key] == current[field]
    return False


def cached_central_directory(
    url: str, path: Path, *, check_version: bool = True, registry: ObjectStoreRegistry | None = None
) -> pa.Table:
    """:func:`read_central_directory`, read from the archive once and kept as Parquet at ``path``.

    Why keep it: a large archive's directory is slow to fetch from far away
    (S-BIAD3228's is 178.7 MB for two million members), and a kept copy
    records what the archive held when it was read. Parquet, so the same file
    opens in polars, pandas or DuckDB for a person exploring the archive.

    - No file at ``path``: the directory is read and written there, with the
      URL and the archive's version (ETag, last-modified time, size) in the
      file's metadata.
    - A file for this ``url`` that matches a ``HEAD`` of the archive, by the
      most specific of those fields both have: the file is used.
    - A file for another URL, or one that no longer matches: raises
      ``ValueError`` rather than read members at offsets that may be stale.
      Delete the file to read the archive again.

    ``check_version=False`` skips the ``HEAD``, so a kept file can be read
    with no network, for views that use only the directory. Anything that
    reads members at the kept offsets should leave it on.
    """
    if path.exists():
        directory = pq.read_table(path)
        recorded = {key.decode(): value.decode() for key, value in (directory.schema.metadata or {}).items()}
        if recorded.get("url") != url:
            raise ValueError(f"{path} holds the directory of {recorded.get('url')}, not {url}")
        if check_version and not _same_version(recorded, archive_version(url, registry=registry)):
            raise ValueError(f"{path} was read from an earlier version of {url}; delete it to read the archive again")
        return directory
    directory = read_central_directory(url, registry=registry)
    directory = directory.replace_schema_metadata({**directory.schema.metadata, b"url": url.encode()})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(directory, path)
    return directory


def read_members(
    url: str,
    names: Iterable[str],
    directory: pa.Table,
    *,
    concurrency: int = 32,
    registry: ObjectStoreRegistry | None = None,
) -> dict[str, bytes]:
    """The uncompressed bytes of each named member, by one range request each.

    For a small set of members, such as the metadata documents of part of a
    zipped Zarr store; requests run ``concurrency`` at a time. Each request
    covers the member's span from
    :func:`~minus80.parsers.zip.directory.member_spans`, and
    :func:`~minus80.parsers.zip.directory.parse_member` reads it.

    Raises ``KeyError`` for a name the directory does not hold.
    """
    spans = member_spans(directory, names)
    entries = directory.take(spans["entry"]).select(["name", "compressed", "crc32"]).to_pylist()
    starts, ends = spans["start"].to_pylist(), spans["end"].to_pylist()
    # A block larger than any member keeps each span to one request.
    fetched = iter_ranges(
        url, list(zip(starts, ends)), block=1 << 40, concurrency=concurrency,
        progress=False, patient=True, registry=registry,
    )
    return {
        entry["name"]: parse_member(member, start, entry)
        for (start, member), entry in zip(fetched, entries, strict=True)
    }
