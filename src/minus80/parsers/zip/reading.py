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

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from obspec_utils.registry import ObjectStoreRegistry

from minus80.core.fetch import iter_ranges, object_size, read_range

from .buffers import end_of_central_directory
from .directory import END_RECORDS, parse_central_directory, parse_member

#: The central directory is fetched in pieces of this size, this many at once.
#: One request for S-BIAD3228's 178.7 MB directory ran for minutes from AWS
#: London with nothing to show; 8 MiB pieces, 8 at once, took about 10 s.
DIRECTORY_PIECE = 8 << 20
DIRECTORY_CONCURRENCY = 8


def read_central_directory(url: str, *, registry: ObjectStoreRegistry | None = None) -> pa.Table:
    """The central directory of the archive at ``url``, one row per member.

    Fetches the archive's last :data:`END_RECORDS` bytes
    to find where the directory starts, then everything from there to the
    end in :data:`DIRECTORY_PIECE` pieces, :data:`DIRECTORY_CONCURRENCY` at a
    time, and parses it with
    :func:`~minus80.parsers.zip.directory.parse_central_directory`. No member
    is read.
    """
    archive_size = object_size(url, patient=True, registry=registry)
    end = read_range(url, max(0, archive_size - END_RECORDS), archive_size, patient=True, registry=registry)
    end_record = end_of_central_directory(end, archive_size)
    archive_end = bytearray()
    for _, piece in iter_ranges(
        url, [(end_record.offset, archive_size)], block=DIRECTORY_PIECE,
        concurrency=DIRECTORY_CONCURRENCY, patient=True, registry=registry,
    ):
        archive_end += piece
    return parse_central_directory(bytes(archive_end), archive_size)


def cached_central_directory(
    url: str, path: Path, *, check_size: bool = True, registry: ObjectStoreRegistry | None = None
) -> pa.Table:
    """:func:`read_central_directory`, read from the archive once and kept as Parquet at ``path``.

    Why keep it: a large archive's directory is slow to fetch from far away
    (S-BIAD3228's is 178.7 MB for two million members), and a kept copy
    records what the archive held when it was read. Parquet, so the same file
    opens in polars, pandas or DuckDB for a person exploring the archive.

    - No file at ``path``: the directory is read and written there, with the
      URL in the file's metadata.
    - A file for this ``url`` whose archive size matches a ``HEAD`` of the
      archive: the file is used.
    - A file for another URL, or an archive whose size has changed: raises
      ``ValueError`` rather than read members at offsets that may be stale.
      Delete the file to read the archive again.

    ``check_size=False`` skips the ``HEAD``, so a kept file can be read with
    no network at all, as when exploring an archive offline. Anything that
    reads members at the kept offsets should leave it on.
    """
    if path.exists():
        directory = pq.read_table(path)
        metadata = {key.decode(): value.decode() for key, value in (directory.schema.metadata or {}).items()}
        if metadata.get("url") != url:
            raise ValueError(f"{path} holds the directory of {metadata.get('url')}, not {url}")
        if not check_size:
            return directory
        archive_size = object_size(url, registry=registry)
        if int(metadata["archive_size"]) != archive_size:
            raise ValueError(f"{path} holds {url} at {metadata['archive_size']} bytes; it is now {archive_size}")
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

    For a small set of members, such as the metadata
    documents of part of a zipped Zarr store; requests run ``concurrency``
    at a time. Each request covers the member from its local header to the
    next member's local header, and
    :func:`~minus80.parsers.zip.directory.parse_member` reads it.

    Raises ``KeyError`` for a name the directory does not hold.
    """
    names = list(names)
    wanted = directory.filter(pc.is_in(directory["name"], pa.array(names, pa.string())))
    entries = {entry["name"]: entry for entry in wanted.to_pylist()}
    missing = [name for name in names if name not in entries]
    if missing:
        raise KeyError(f"not in the archive: {missing[:5]}")
    header_offsets = np.sort(directory["local_header_offset"].to_numpy())
    directory_offset = int(directory.schema.metadata[b"directory_offset"])
    spans = []
    for name in names:
        start = entries[name]["local_header_offset"]
        following = int(np.searchsorted(header_offsets, start, side="right"))
        end = int(header_offsets[following]) if following < len(header_offsets) else directory_offset
        spans.append(((start, end), name))
    spans.sort()
    # A block larger than any member keeps each span to one request.
    fetched = iter_ranges(
        url, [span for span, _ in spans], block=1 << 40, concurrency=concurrency,
        progress=False, patient=True, registry=registry,
    )
    return {
        name: parse_member(member, start, entries[name])
        for (start, member), (_, name) in zip(fetched, spans, strict=True)
    }
