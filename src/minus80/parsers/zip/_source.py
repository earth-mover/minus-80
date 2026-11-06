"""Byte reads from a zip source: a URL, a local path, or an open binary file.

URLs and paths go through :mod:`minus80.core.fetch`. An open file is read
with ``seek`` and ``read``, since core reads only what it can address by URL.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from typing import BinaryIO

from minus80.core.fetch import iter_ranges, object_size, read_range

Source = str | os.PathLike | BinaryIO


def is_file(source: Source) -> bool:
    return hasattr(source, "read")


def size(source: Source, *, patient: bool = False) -> int:
    if is_file(source):
        return source.seek(0, os.SEEK_END)
    return object_size(os.fspath(source), patient=patient)


def read(source: Source, start: int, end: int, *, patient: bool = False) -> bytes:
    """Bytes ``[start, end)`` of ``source``; raises ``OSError`` on a short read, as core does."""
    if not is_file(source):
        return read_range(os.fspath(source), start, end, patient=patient)
    source.seek(start)
    data = source.read(end - start)
    if len(data) != end - start:
        raise OSError(f"short read of bytes [{start}, {end}): got {len(data)} of {end - start}")
    return data


def ranges(source: Source, spans: Iterable[tuple[int, int]], *, block: int, concurrency: int,
           progress: bool, patient: bool = False) -> Iterator[tuple[int, bytes]]:
    """Each span as ``(start, data)`` in pieces of at most ``block`` bytes, as :func:`~minus80.core.fetch.iter_ranges` yields them."""
    if not is_file(source):
        yield from iter_ranges(os.fspath(source), spans, block=block, concurrency=concurrency,
                               progress=progress, patient=patient)
        return
    for start, end in spans:
        for s in range(start, end, block):
            yield s, read(source, s, min(s + block, end))


class FileStore:
    """The obspec read calls ``parse_zip_index`` makes, answered from an open binary file."""

    def __init__(self, f: BinaryIO) -> None:
        self._f = f

    async def head_async(self, path: str) -> dict:
        return {"size": size(self._f)}

    async def get_range_async(self, path: str, *, start: int, end: int) -> bytes:
        return read(self._f, start, end)

    async def get_ranges_async(self, path: str, *, starts, ends) -> list[bytes]:
        return [read(self._f, s, e) for s, e in zip(starts, ends)]
