"""A zipped Zarr store opened in place, member by member, for reading its decoded values.

A ``.zarr.zip`` whose members are Deflate-compressed cannot be referenced
virtually (see this package's README), so an ingest that wants its pixels has
to read them. :class:`MemberStore` lets zarr do that reading: it is a read-only
zarr store whose keys are the archive's member names, and each ``get`` fetches
one member by a range request and inflates it. zarr then does everything else,
opening the store's own metadata, working out which chunks a selection needs,
and decoding them with the store's own codecs.

VirtualiZarr's ``_ZipMetadataStore`` is the same idea for STORED members only;
docs/upstream.md tracks an inflating store upstream.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator, Iterable, Mapping

from virtualizarr.parsers.zarr.zip import ZipEntry
from zarr.abc.store import ByteRequest, OffsetByteRequest, RangeByteRequest, Store, SuffixByteRequest
from zarr.core.buffer import Buffer, BufferPrototype

from minus80.core.registry import store_and_key

from .members import member_bytes


class MemberStore(Store):
    """The archive at ``url`` as a read-only zarr store, decoding each member as it is read.

    ``index`` maps member names to their entries, from
    :func:`~minus80.parsers.zip.members.zip_index` or a subset of it: a store
    built over one image's members can open that image and nothing else, which
    keeps what a worker is sent small. Each member is fetched by one GET
    through the patient store :func:`~minus80.core.registry.store_and_key`
    builds, inflated on a worker thread, and must inflate to the length its
    entry states, or ``OSError`` is raised.
    """

    def __init__(self, url: str, index: Mapping[str, ZipEntry]) -> None:
        super().__init__(read_only=True)
        self.url = url
        self._index = dict(index)
        self._store, self._key = store_and_key(url, patient=True)

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, MemberStore) and other.url == self.url
                and other._index == self._index)

    async def get(self, key: str, prototype: BufferPrototype,
                  byte_range: ByteRequest | None = None) -> Buffer | None:
        entry = self._index.get(key)
        if entry is None:
            return None
        stored = await self._store.get_range_async(
            self._key, start=entry.data_offset, end=entry.data_offset + entry.compressed_length)
        stored = bytes(stored)
        if len(stored) != entry.compressed_length:
            raise OSError(f"{self.url} {key}: got {len(stored)} of {entry.compressed_length} bytes")
        try:
            # zlib releases the GIL, so inflating on a thread lets the many
            # members zarr fetches at once inflate on as many cores.
            data = await asyncio.to_thread(member_bytes, stored, entry)
        except zlib.error as e:
            raise OSError(f"{self.url} {key}: {e}") from e
        if len(data) != entry.uncompressed_length:
            raise OSError(f"{self.url} {key}: inflated to {len(data)} bytes, "
                          f"the archive says {entry.uncompressed_length}")
        return prototype.buffer.from_bytes(_slice(data, byte_range))

    async def get_partial_values(self, prototype: BufferPrototype,
                                 key_ranges: Iterable[tuple[str, ByteRequest | None]]
                                 ) -> list[Buffer | None]:
        return await asyncio.gather(*(self.get(k, prototype, r) for k, r in key_ranges))

    async def exists(self, key: str) -> bool:
        return key in self._index

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
        for key in self._index:
            yield key

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        for key in self._index:
            if key.startswith(prefix):
                yield key

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        prefix = prefix.rstrip("/")
        seen: set[str] = set()
        for key in self._index:
            if prefix and not key.startswith(prefix + "/"):
                continue
            child = (key[len(prefix) + 1:] if prefix else key).split("/")[0]
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
        return data[byte_range.start:byte_range.end]
    if isinstance(byte_range, OffsetByteRequest):
        return data[byte_range.offset:]
    if isinstance(byte_range, SuffixByteRequest):
        return data[len(data) - byte_range.suffix:]
    raise TypeError(f"unknown byte range {byte_range!r}")
