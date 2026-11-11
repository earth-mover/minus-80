"""Copy Zarr v2 arrays from a zip into a Zarr v3 group, chunk bytes unchanged.

Zarr v3's blosc, transpose and vlen-utf8 codecs read v2 chunk bytes as they are.
Only the metadata is rewritten. A v2 feature with no v3 equivalent raises
``NotImplementedError``, because a chunk that decodes wrong is worse than no copy.
"""

from __future__ import annotations

import asyncio
import json
import re
import zipfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import zarr
from zarr.codecs import BloscCodec, TransposeCodec, VLenUTF8Codec
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from .jsonable import jsonable

_SHUFFLE = {0: "noshuffle", 1: "shuffle", 2: "bitshuffle"}
_CHUNK_NAME = re.compile(r"\d+(?:[./]\d+)*")


@dataclass(frozen=True)
class ArraySpec:
    """The parts of a v2 ``.zarray`` that a v3 array needs."""

    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    dtype: np.dtype | str  # "str" for a vlen-utf8 object array
    fill_value: Any
    order: str
    compressor: Mapping | None
    filters: list[Mapping] = field(default_factory=list)  # all but vlen-utf8


def v2_spec(zarray: Mapping) -> ArraySpec:
    """Read a v2 ``.zarray`` document."""
    filters = list(zarray.get("filters") or [])
    dtype = np.dtype(zarray["dtype"])
    if dtype.byteorder == ">":
        raise NotImplementedError(f"big-endian dtype {zarray['dtype']!r}")
    if dtype == object:
        if not any(f["id"] == "vlen-utf8" for f in filters):
            raise NotImplementedError(
                f"object dtype with filters {[f['id'] for f in filters]}"
            )
        filters = [f for f in filters if f["id"] != "vlen-utf8"]
        dtype = "str"
    return ArraySpec(
        shape=tuple(zarray["shape"]),
        chunks=tuple(zarray["chunks"]),
        dtype=dtype,
        fill_value=zarray.get("fill_value"),
        order=zarray.get("order", "C"),
        compressor=zarray.get("compressor"),
        filters=filters,
    )


def _compressor_codec(compressor: Mapping | None) -> BloscCodec | None:
    if compressor is None:
        return None
    if compressor["id"] != "blosc":
        raise NotImplementedError(f"compressor {compressor['id']!r}")
    shuffle = compressor.get("shuffle", 1)
    if shuffle not in _SHUFFLE:
        raise NotImplementedError(f"blosc shuffle {shuffle!r}")
    return BloscCodec(
        cname=compressor["cname"],
        clevel=compressor["clevel"],
        shuffle=_SHUFFLE[shuffle],
        blocksize=compressor.get("blocksize", 0),
    )


def create_copy(
    group: zarr.Group,
    name: str,
    zarray: Mapping,
    *,
    dimension_names: Sequence[str] | None = None,
    attributes: Mapping | None = None,
) -> zarr.Array:
    """Create the v3 array that reads the chunks of the v2 array ``zarray`` describes."""
    spec = v2_spec(zarray)
    if spec.filters:
        raise NotImplementedError(f"filter {spec.filters[0]['id']!r}")
    compressor = _compressor_codec(spec.compressor)
    is_str = spec.dtype == "str"
    fill_value = spec.fill_value
    if is_str and fill_value is None:
        fill_value = ""
    return group.create_array(
        name,
        shape=spec.shape,
        chunks=spec.chunks,
        dtype=spec.dtype,
        fill_value=fill_value,
        filters=[TransposeCodec(order=tuple(reversed(range(len(spec.shape)))))]
        if spec.order == "F"
        else None,
        serializer=VLenUTF8Codec() if is_str else "auto",
        compressors=compressor,
        dimension_names=dimension_names,
        attributes=None if attributes is None else jsonable(attributes),
    )


def copy_chunks(
    zf: zipfile.ZipFile,
    prefix: str,
    array: zarr.Array,
    *,
    concurrency: int = 1,
    names: Collection[str] | None = None,
) -> int:
    """Write each chunk member under ``prefix`` to its v3 key. Returns the count.

    Members are read one at a time; at most ``concurrency`` writes run at once.
    ``names`` is the member set of ``zf``, for a caller that listed the zip already.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")
    stem = f"{prefix}/" if prefix else ""
    prototype = default_buffer_prototype()
    keys = {}
    for member in zf.namelist() if names is None else names:
        index = member.removeprefix(stem)
        if not member.startswith(stem) or not _CHUNK_NAME.fullmatch(index):
            continue
        # A v2 array of zero dimensions has the one chunk "0"; v3 names it "c".
        keys[member] = (
            f"{array.path}/c"
            if array.ndim == 0
            else f"{array.path}/c/" + index.replace(".", "/")
        )

    async def put_all() -> None:
        free = asyncio.Semaphore(concurrency)
        inflight: set[asyncio.Task] = set()
        errors: list[BaseException] = []

        async def put(key: str, data: bytes) -> None:
            await array.store.set(key, prototype.buffer.from_bytes(data))

        def settled(task: asyncio.Task) -> None:
            inflight.discard(task)
            free.release()
            if not task.cancelled() and task.exception() is not None:
                errors.append(task.exception())

        try:
            for member, key in keys.items():
                await free.acquire()
                # A failed write ends the loop before it reads more members.
                if errors:
                    break
                task = asyncio.create_task(put(key, zf.read(member)))
                inflight.add(task)
                task.add_done_callback(settled)
            if inflight:
                await asyncio.wait(set(inflight), return_when=asyncio.FIRST_EXCEPTION)
        finally:
            # Nothing may run on the background loop after this function returns.
            rest = list(inflight)
            for task in rest:
                task.cancel()
            await asyncio.gather(*rest, return_exceptions=True)
        if errors:
            raise errors[0]

    sync(put_all())
    return len(keys)


def copy_group(
    zf: zipfile.ZipFile,
    prefix: str,
    group: zarr.Group,
    *,
    dimension_names: Mapping[str, Sequence[str]] | None = None,
    attributes: bool = True,
    concurrency: int = 1,
) -> dict[str, int]:
    """Copy every v2 array under ``prefix`` into ``group``. Returns chunk counts by path."""
    stem = f"{prefix}/" if prefix else ""
    counts: dict[str, int] = {}
    names = set(zf.namelist())
    if attributes:
        for member in sorted(names):
            directory = member.removesuffix(".zattrs").removesuffix("/")
            if (
                not member.endswith(".zattrs")
                or f"{directory}/.zarray".lstrip("/") in names
            ):
                continue
            if directory == prefix or directory.startswith(stem):
                path = directory.removeprefix(stem) if directory != prefix else ""
                target = group.require_group(path) if path else group
                target.attrs.update(jsonable(json.loads(zf.read(member))))
    for member in zf.namelist():
        if not member.startswith(stem) or not member.endswith(".zarray"):
            continue
        array_prefix = member.removesuffix("/.zarray").removesuffix(".zarray")
        path = array_prefix.removeprefix(stem)
        zattrs = f"{array_prefix}/.zattrs" if array_prefix else ".zattrs"
        attrs = (
            json.loads(zf.read(zattrs))
            if attributes and zattrs in zf.namelist()
            else None
        )
        array = create_copy(
            group,
            path,
            json.loads(zf.read(member)),
            dimension_names=(dimension_names or {}).get(path),
            attributes=attrs,
        )
        counts[path] = copy_chunks(
            zf, array_prefix, array, concurrency=concurrency, names=names
        )
    return counts
