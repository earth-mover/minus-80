"""Read a zipped Zarr store (``.zarr.zip``) in place, through zarr, without unzipping it.

Why this exists
---------------
Some archives publish a Zarr store as one zip file. The BioImage Archive's
S-BIAD3228 is a 591 GB ``.zarr.zip`` with two million members, each chunk
compressed once by the Zarr codec (blosc) and again by the zip (Deflate).
Such a zip cannot be referenced virtually (see this package's README), so an
ingest has to read its pixels. :class:`MemberStore` lets zarr do that reading,
so zarr still works out which chunks a selection needs and decodes them.

Why not zarr's ``ZipStore`` or fsspec's zip filesystem
-------------------------------------------------------
Both work, and both are built on Python's ``zipfile``, which is slow for
this job in two ways:

- **One read at a time.** ``zipfile.ZipFile`` takes one lock around every
  read of its file (``zipfile.py``, ``_SharedFile.read``), and zarr's
  ``ZipStore`` adds its own lock around every ``get`` (``zarr/storage/_zip.py``,
  zarr 3.4). zarr asks for dozens of chunks at once; through either store they
  are fetched in sequence, one network round trip each.
- **A Python object per member, before the first read.** Opening the zip
  parses its whole table of contents into ``ZipInfo`` objects: two million of
  them for S-BIAD3228, about 1 GB and tens of seconds, in every process.

:class:`MemberStore` fetches each member with its own range request, so
zarr's concurrent reads really are concurrent, and it takes the table of
contents as the central directory's table (from
:func:`~minus80.parsers.zip.reading.read_central_directory`), which a caller
reads once and can keep.

It suits reading part of a store, such as a sample of chunks to check. To
copy a whole store, :func:`~minus80.parsers.zip.reading.stream_members` reads
each byte once in large ranges instead of one request per member.

VirtualiZarr has the same idea as a private class, ``_ZipMetadataStore``, but
it returns members as stored, so it cannot read Deflate members.
docs/upstream.md tracks an inflating store upstream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable

import pyarrow as pa
from zarr.abc.store import (
    ByteRequest,
    OffsetByteRequest,
    RangeByteRequest,
    Store,
    SuffixByteRequest,
)
from zarr.core.buffer import Buffer, BufferPrototype

from minus80.core.registry import store_and_key

from .directory import member_spans, parse_member
from .zarr_v2 import METADATA_NAMES

#: The file names Zarr uses for metadata: Zarr v2's, and ``zarr.json`` in Zarr v3.
ZARR_METADATA = (*METADATA_NAMES, "zarr.json")


def metadata_names(names: Iterable[str], *, also: Iterable[str] = ()) -> list[str]:
    """The names of a zipped Zarr store's metadata documents, without its chunks.

    zarr finds a group's children by scanning every key in its store. In a
    large zipped store almost every member is a chunk: S-BIAD3228 has two
    million members and only a few thousand metadata documents. Reading just
    these, with :func:`~minus80.parsers.zip.reading.read_members`, into an
    in-memory store lets a reader walk the hierarchy and read attributes
    quickly.

    ``also`` names path segments whose members are kept too, chunks included.
    Use it for small arrays the reader needs in full, such as the AnnData
    tables a Fractal plate keeps under ``tables``.
    """
    keep = [f"/{segment}/" for segment in also]
    return [
        name
        for name in names
        if name.rsplit("/", 1)[-1] in ZARR_METADATA or any(k in f"/{name}" for k in keep)
    ]


class MemberStore(Store):
    """A ``.zarr.zip`` at ``url``, read by zarr without unzipping it.

    Why it is needed: zarr reads a chunk by asking its store for a key, such
    as ``C/02/1/0/0/13/0/7``. In a zipped store that key is not an object in
    the bucket; it is a member packed inside the one zip file, and usually
    compressed a second time by the zip. An ordinary store has nothing to
    return for it, and zarr's own ``ZipStore`` opens only local files.

    What a ``get`` does, for each key zarr asks for:

    - looks the key up in ``directory``, the zip's central directory, for the
      member's span: from its local header to the next member's;
    - fetches the span with one range request;
    - reads the local header from those bytes, inflates the member on a
      worker thread, so many members inflate at once, and checks it against
      the directory's CRC (:func:`~minus80.parsers.zip.directory.parse_member`);
    - treats an empty member as absent, so zarr reads the fill value there.
      Every Zarr codec writes at least a header, so an empty member cannot
      be a chunk;
    - passes the member to ``check``, when one is given. ``check`` returns
      None for a member that can be decoded, or the reason it cannot, such
      as a truncated frame. A member that fails is treated as absent too, and
      its name and reason are kept in :attr:`rejected` for the caller to
      record.

    zarr then decodes the chunk with the array's own codecs, as it would from
    any other store.
    """

    def __init__(
        self,
        url: str,
        directory: pa.Table,
        *,
        check: Callable[[str, bytes], str | None] | None = None,
    ) -> None:
        super().__init__(read_only=True)
        self.url = url
        spans = member_spans(directory)
        rows = directory.take(spans["entry"]).select(["name", "compressed", "uncompressed", "method", "crc32"])
        #: Each member's directory row, with its span as ``start`` and ``end``, by name.
        self._members = {
            row["name"]: {**row, "start": start, "end": end}
            for row, start, end in zip(rows.to_pylist(), spans["start"].to_pylist(), spans["end"].to_pylist())
        }
        self._check = check
        #: Members ``check`` rejected, by name, with the reason.
        self.rejected: dict[str, str] = {}
        self._store, self._key = store_and_key(url, patient=True)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MemberStore) and other.url == self.url and other._members == self._members

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        member = self._members.get(key)
        # An empty member holds no chunk: every Zarr codec writes at least a
        # header. Treating it as absent makes zarr read the fill value, as for
        # any missing chunk, instead of failing to decode zero bytes.
        # S-BIAD3228 has 1,244 such members. The caller records which.
        if member is None or member["uncompressed"] == 0:
            return None
        span = bytes(await self._store.get_range_async(self._key, start=member["start"], end=member["end"]))
        if len(span) != member["end"] - member["start"]:
            raise OSError(f"{self.url} {key}: got {len(span)} of {member['end'] - member['start']} bytes")
        try:
            # zlib releases the GIL, so inflating on a thread lets the many
            # members zarr fetches at once inflate on as many cores.
            data = await asyncio.to_thread(parse_member, span, member["start"], member)
        except ValueError as e:
            raise OSError(f"{self.url} {key}: {e}") from e
        if self._check is not None and (reason := self._check(key, data)) is not None:
            self.rejected[key] = reason
            return None
        return prototype.buffer.from_bytes(_slice(data, byte_range))

    async def get_partial_values(
        self, prototype: BufferPrototype, key_ranges: Iterable[tuple[str, ByteRequest | None]]
    ) -> list[Buffer | None]:
        return await asyncio.gather(*(self.get(k, prototype, r) for k, r in key_ranges))

    async def exists(self, key: str) -> bool:
        member = self._members.get(key)
        return member is not None and member["uncompressed"] > 0

    @property
    def supports_writes(self) -> bool:
        return False

    async def set(self, key: str, value: Buffer) -> None:
        raise NotImplementedError("a zip archive is read-only")

    @property
    def supports_deletes(self) -> bool:
        return False

    async def delete(self, key: str) -> None:
        raise NotImplementedError("a zip archive is read-only")

    @property
    def supports_listing(self) -> bool:
        return True

    async def list(self) -> AsyncIterator[str]:
        for key in self._members:
            yield key

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        for key in self._members:
            if key.startswith(prefix):
                yield key

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        prefix = prefix.rstrip("/")
        seen: set[str] = set()
        for key in self._members:
            if prefix and not key.startswith(prefix + "/"):
                continue
            child = (key[len(prefix) + 1 :] if prefix else key).split("/")[0]
            if child and child not in seen:
                seen.add(child)
                yield child


def _slice(data: bytes, byte_range: ByteRequest | None) -> bytes:
    """The part of a member's uncompressed bytes that ``byte_range`` asks for.

    A range applies to the decoded member, so the whole member is fetched and
    inflated first: a Deflate stream cannot be entered part way through.
    """
    if byte_range is None:
        return data
    if isinstance(byte_range, RangeByteRequest):
        return data[byte_range.start : byte_range.end]
    if isinstance(byte_range, OffsetByteRequest):
        return data[byte_range.offset :]
    if isinstance(byte_range, SuffixByteRequest):
        return data[len(data) - byte_range.suffix :]
    raise TypeError(f"unknown byte range {byte_range!r}")
