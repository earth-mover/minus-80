"""The chunks of a zipped Zarr v2 store, decoded from members as they stream past.

Why this exists
---------------
A zipped Zarr store (``.zarr.zip``) is a Zarr v2 store whose keys are the
zip's member names: ``C/02/2/0/.zarray`` is an array's metadata and
``C/02/2/0/1/13/0/7`` is one of its chunks. zarr reads such a store by asking
for one key at a time, which is the right model for reading part of an array
and the wrong one for copying all of it: :mod:`minus80.parsers.zip.streaming`
delivers members in the order the zip stores them, and each member has to be
decoded as it arrives. zarr has no public call that decodes a chunk's bytes
given an array's metadata, so this module does that with numcodecs, the codec
library Zarr v2 itself names in every ``.zarray``.

What it does, on bytes only
---------------------------
- :func:`array_paths` and :func:`chunk_key` say what a member is: a metadata
  document, or chunk ``(i, j, ...)`` of a given array. The directory lists
  every ``.zarray``, so the arrays are known before any chunk arrives.
- :class:`ArraySpec` holds what a ``.zarray`` says, and
  :meth:`ArraySpec.same_encoding` compares two arrays' encodings. A caller
  can decode an array's chunks with the encoding of another array before the
  array's own ``.zarray`` arrives, and check it when it does.
- :func:`decode_chunk` decodes one chunk; :func:`frame_problem` finds a Blosc
  frame cut short without decoding it.

It lives beside the zip modules because only a zipped store needs it: a Zarr
store in a directory or bucket is read by zarr directly.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import NamedTuple

import numcodecs
import numpy as np

#: The file names Zarr v2 uses for metadata.
METADATA_NAMES = (".zarray", ".zattrs", ".zgroup")


def is_metadata(name: str) -> bool:
    """True for a Zarr v2 metadata document: ``.zarray``, ``.zattrs`` or ``.zgroup``."""
    return name.rsplit("/", 1)[-1] in METADATA_NAMES


def array_paths(names: Iterable[str]) -> set[str]:
    """The paths of the store's arrays: every key that has a ``.zarray`` beneath it."""
    return {name.rsplit("/", 1)[0] if "/" in name else "" for name in names if name.rsplit("/", 1)[-1] == ".zarray"}


class ChunkKey(NamedTuple):
    """Which chunk of which array a member holds."""

    array: str
    #: The chunk's position in the array's chunk grid, one integer per axis.
    index: tuple[int, ...]


def chunk_key(name: str, arrays: set[str]) -> ChunkKey | None:
    """The array and chunk index a member's name addresses, or None if it is not a chunk.

    Zarr v2 joins a chunk's indices with the array's ``dimension_separator``,
    ``/`` (``a/1/13/0/7``) or ``.`` (``a/1.13.0.7``); both are recognized. The
    longest array path that the name starts with is the array.
    """
    parts = name.split("/")
    for split in range(len(parts) - 1, -1, -1):
        prefix = "/".join(parts[:split])
        if prefix not in arrays:
            continue
        rest = parts[split:]
        if len(rest) == 1 and "." in rest[0]:
            rest = rest[0].split(".")
        if rest and all(part.isdigit() for part in rest):
            return ChunkKey(prefix, tuple(int(part) for part in rest))
        return None
    return None


@dataclass(frozen=True)
class ArraySpec:
    """What a Zarr v2 ``.zarray`` says about an array's shape and how its chunks are encoded."""

    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    dtype: np.dtype
    #: The numcodecs configuration of the compressor, or None.
    compressor: Mapping | None
    filters: tuple[Mapping, ...]
    fill_value: object
    order: str

    @classmethod
    def from_document(cls, document: bytes | str | Mapping) -> ArraySpec:
        """Read a ``.zarray`` document, as bytes, text or a parsed mapping."""
        meta = document if isinstance(document, Mapping) else json.loads(document)
        if meta.get("zarr_format") != 2:
            raise ValueError(f"not a Zarr v2 array: zarr_format {meta.get('zarr_format')}")
        return cls(
            shape=tuple(meta["shape"]),
            chunks=tuple(meta["chunks"]),
            dtype=np.dtype(meta["dtype"]),
            compressor=meta.get("compressor"),
            filters=tuple(meta.get("filters") or ()),
            fill_value=meta.get("fill_value"),
            order=meta.get("order", "C"),
        )

    def encoding(self) -> tuple:
        """Everything that decides how a chunk's bytes decode: all but the array's shape."""
        return (
            self.chunks,
            self.dtype.str,
            json.dumps(self.compressor, sort_keys=True),
            json.dumps(self.filters, sort_keys=True),
            self.order,
            json.dumps(self.fill_value),
        )

    def same_encoding(self, other: ArraySpec) -> bool:
        """True if chunks of either array decode the same way."""
        return self.encoding() == other.encoding()

    def grid(self) -> tuple[int, ...]:
        """How many chunks the array has along each axis."""
        return tuple(math.ceil(n / c) for n, c in zip(self.shape, self.chunks, strict=True))


def decode_chunk(data: bytes, spec: ArraySpec) -> np.ndarray:
    """One chunk's values, from its encoded bytes, shaped as the array's chunk.

    The compressor and filters are numcodecs codecs, as Zarr v2 defines them;
    filters are undone in reverse order after decompressing. numcodecs'
    Blosc and zlib release the GIL, so many chunks decode at once on a thread
    pool.

    Raises ``ValueError`` if the decoded bytes are not one chunk's worth.
    """
    if spec.dtype.hasobject:
        raise ValueError("chunks of object dtype need an object codec; not supported")
    buffer = numcodecs.get_codec(dict(spec.compressor)).decode(data) if spec.compressor else data
    for config in reversed(spec.filters):
        buffer = numcodecs.get_codec(dict(config)).decode(buffer)
    values = np.frombuffer(buffer, dtype=spec.dtype)
    if values.size != math.prod(spec.chunks):
        raise ValueError(f"decoded {values.size} values, a chunk holds {math.prod(spec.chunks)}")
    return values.reshape(spec.chunks, order=spec.order)


def frame_problem(data: bytes, compressor: Mapping | None) -> str | None:
    """Why a chunk's bytes cannot be decoded, found without decoding them, or None.

    Only Blosc frames are checked: a Blosc frame states its own length in
    bytes 12 to 16 of its header, so a frame cut short shows at once. Any
    other compressor returns None, and :func:`decode_chunk` finds a broken
    chunk when it fails. ``compressor`` is the array's, as in
    :attr:`ArraySpec.compressor`.
    """
    if not compressor or compressor.get("id") != "blosc":
        return None
    if len(data) < 16:
        return f"{len(data)} bytes, shorter than a Blosc header"
    stated = int.from_bytes(data[12:16], "little")
    if stated != len(data):
        return f"truncated Blosc frame: {len(data):,} of {stated:,} bytes"
    return None
