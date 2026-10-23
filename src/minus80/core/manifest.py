"""Virtual arrays whose chunks are a source's own stored units.

Every parser ends the same way: a grid of ``(path, offset, length)``, one entry
per tile, strip, frame or member, restated as a VirtualiZarr ``ManifestArray``.
:func:`manifest_array` is that step, so the rules for it (what a missing chunk
is, what attributes may hold, which file the array names as its source) are
applied in one place.

:func:`stack`, :func:`concatenate` and :func:`permute_dims` build an ingest's
arrays from parsed ones, by dimension name, with attributes that still
describe the result.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
import numpy.typing as npt
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.manifests.manifest import MISSING_CHUNK_PATH
from virtualizarr.manifests.utils import create_v3_array_metadata, manifest_chunk_shape
from zarr.core.metadata.v3 import ArrayV3Metadata

from .jsonable import jsonable

MISSING = MISSING_CHUNK_PATH

#: Attribute keys whose value is a mapping keyed by dimension name.
_PER_DIMENSION = ("scale", "units", "translation", "dimensions")


def manifest_array(
    paths: str | np.ndarray,
    offsets: npt.ArrayLike,
    lengths: npt.ArrayLike,
    *,
    shape: Sequence[int],
    chunk_shape: Sequence[int],
    dtype: npt.DTypeLike,
    codecs: Sequence[Mapping],
    dimension_names: Sequence[str | None] | None = None,
    fill_value=0,
    attributes: Mapping | None = None,
) -> ManifestArray:
    """A virtual array whose chunk at each grid position is one stored unit of a source.

    ``offsets`` and ``lengths`` are shaped like the chunk grid
    (``ceil(shape / chunk_shape)`` per axis); a mismatch raises ``ValueError``.
    ``paths`` is one URL for every chunk, or an array of URLs shaped like the
    grid. Each entry is recorded as given: no URL is normalized here.

    A chunk with length 0, or whose path is ``MISSING`` or ``None``, is a
    missing chunk: its path is set to ``MISSING`` and its offset and length to
    0, and it reads as ``fill_value``. Icechunk rejects a zero-length
    reference, so a unit with no stored bytes cannot be anything else.

    ``attributes`` pass through :func:`minus80.core.jsonable.jsonable`.
    Physical metadata uses these keys, each mapping keyed by dimension name:
    ``scale`` (size of one element), ``units`` (OME-Zarr unit names such as
    ``"micrometer"``), ``translation``, and ``dimensions``, whose value for
    each axis is a mapping with a ``type`` string: ``"space"``, ``"time"``,
    ``"channel"`` or another name, such as ``"fov"``, for what the axis
    measures. ``channel_names`` is a list in channel-axis order. A key of any
    of the four mappings that is not a dimension name raises ``ValueError``,
    and a ``dimensions`` entry without a ``type`` string raises ``TypeError``.

    ``source_url`` is set to the distinct non-missing URLs the manifest
    references (a string for one, a sorted list for several), so the array
    satisfies ``minus80.ome.verify.provenance`` by construction. A caller's
    own ``source_url`` is overwritten, since it could otherwise drift from
    the manifest. An array with no stored chunk references no file and gets
    none.
    """
    shape = tuple(int(s) for s in shape)
    chunk_shape = tuple(int(c) for c in chunk_shape)
    grid = tuple(math.ceil(s / c) for s, c in zip(shape, chunk_shape))
    offsets = np.asarray(offsets, dtype=np.uint64)
    lengths = np.asarray(lengths, dtype=np.uint64)
    for name, a in (("offsets", offsets), ("lengths", lengths)):
        if a.shape != grid:
            raise ValueError(f"{name} have shape {a.shape}, the chunk grid is {grid}")
    if isinstance(paths, str):
        urls = np.full(grid, paths, dtype=object)
    else:
        urls = np.asarray(paths, dtype=object)
        if urls.shape != grid:
            raise ValueError(f"paths have shape {urls.shape}, the chunk grid is {grid}")
    missing = (lengths == 0) | np.vectorize(lambda p: p is None or p == MISSING, otypes=[bool])(urls)
    urls[missing] = MISSING
    offsets = np.where(missing, np.uint64(0), offsets)
    lengths = np.where(missing, np.uint64(0), lengths)
    manifest = ChunkManifest.from_arrays(
        paths=urls.astype(np.dtypes.StringDType()), offsets=offsets, lengths=lengths)

    attrs = jsonable(dict(attributes or {}))
    _check_per_dimension(attrs, dimension_names)
    attrs.pop("source_url", None)
    if sources := source_urls(manifest):
        attrs["source_url"] = sources
    metadata = create_v3_array_metadata(
        shape=shape, data_type=np.dtype(dtype), chunk_shape=chunk_shape,
        fill_value=fill_value, codecs=[dict(c) for c in codecs],
        dimension_names=None if dimension_names is None else tuple(dimension_names),
        attributes=attrs)
    return ManifestArray(metadata=metadata, chunkmanifest=manifest)


def _check_per_dimension(attrs: Mapping, dimension_names: Sequence[str | None] | None) -> None:
    dims = set(dimension_names or ())
    for key in _PER_DIMENSION:
        if stray := set(attrs.get(key) or {}) - dims:
            raise ValueError(f"{key} names {sorted(stray)}, which are not dimensions of "
                             f"{tuple(dimension_names or ())}")
    for name, axis in (attrs.get("dimensions") or {}).items():
        if not isinstance(axis, dict) or not isinstance(axis.get("type"), str):
            raise TypeError(f"dimensions[{name!r}] is {axis!r}; each axis needs a 'type' string")


def source_urls(manifest: ChunkManifest) -> str | list[str]:
    """The distinct non-missing URLs ``manifest`` references: a string for one, a sorted list for several."""
    found = sorted(set(manifest.iter_nonempty_paths()))
    return found[0] if len(found) == 1 else found


def channel_last_transpose(rank: int) -> dict:
    """The ``transpose`` codec for an array declared channel-first over bytes stored channel-last.

    The array's axes are ``(*leading, c, y, x)`` and each chunk is stored
    ``(*leading, y, x, c)``. ``rank`` 3 gives order ``[1, 2, 0]``; rank 4
    gives ``[0, 2, 3, 1]``. Prepend it to the codec chain.

    Zarr v3's ``transpose`` is an array-to-array codec: it declares the stored
    order a permutation of the array's, so a conformant reader permutes on
    decode and the stored bytes are never rewritten.
    """
    if rank < 3:
        raise ValueError(f"rank {rank}: a channel-last chunk has at least (y, x, c)")
    order = [*range(rank - 3), rank - 2, rank - 1, rank - 3]
    return {"name": "transpose", "configuration": {"order": order}}


def stack(arrays: Sequence[ManifestArray], *, dim: str, axis: Mapping | None = None,
          position: int = 0) -> ManifestArray:
    """``arrays`` stacked along a new axis named ``dim``, inserted at ``position``.

    ``axis`` is the new axis's entry in the ``dimensions`` attribute, such as
    ``{"type": "fov"}``. Every input must have the same dimension names and the
    same attributes apart from ``source_url``; a difference raises
    ``ValueError``, because the result can state only one value. Its
    ``source_url`` lists every file the merged manifest references.

    ``np.stack`` alone gives the new axis no name and copies the first array's
    attributes, ``source_url`` included.
    """
    names, attrs = _common(arrays)
    if dim in names:
        raise ValueError(f"{dim!r} is already a dimension of {names}")
    position = position if position >= 0 else len(names) + 1 + position
    if not 0 <= position <= len(names):
        raise ValueError(f"position {position} is outside the {len(names) + 1} result axes")
    stacked = np.stack(list(arrays), axis=position)
    new_names = (*names[:position], dim, *names[position:])
    if axis is not None:
        attrs["dimensions"] = {**attrs.get("dimensions", {}), dim: jsonable(dict(axis))}
    return _replaced(stacked, names=new_names, attributes=attrs)


def concatenate(arrays: Sequence[ManifestArray], *, dim: str) -> ManifestArray:
    """``arrays`` joined end to end along their existing axis ``dim``.

    The inputs must share dimension names and every attribute but
    ``source_url``, as for :func:`stack`, including ``translation``: the
    result can carry only one, and the first input's is right only when the
    others follow on from it. VirtualiZarr's ``np.concatenate`` checks the
    codecs, the chunk shapes and that only the last input may end in a
    partial chunk along ``dim``.
    """
    names, attrs = _common(arrays)
    if dim not in names:
        raise ValueError(f"{dim!r} is not a dimension of {names}")
    joined = np.concatenate(list(arrays), axis=names.index(dim))
    return _replaced(joined, names=names, attributes=attrs)


def permute_dims(array: ManifestArray, order: Sequence[str | int]) -> ManifestArray:
    """``array`` with its axes reordered; ``order`` names every axis, by name or index.

    The chunk grid is permuted, so every chunk still references the same
    bytes. Those bytes keep the order they were stored in, which a chunk's
    new axis order describes unchanged only when the chunk's axes longer than
    one keep their relative order: one plane per chunk of ``(z, c, y, x)``
    read as ``(c, z, y, x)``, say. Otherwise a Zarr v3 ``transpose`` codec is
    put at the front of the chain, merged with one already there, to declare
    the stored order. A sharded array raises ``NotImplementedError``.
    """
    names = array.metadata.dimension_names or (None,) * array.ndim
    perm = [names.index(o) if isinstance(o, str) else int(o) for o in order]
    if sorted(perm) != list(range(array.ndim)):
        raise ValueError(f"order {tuple(order)} is not a permutation of the axes {names}")
    meta = array.metadata.to_dict()
    codecs = [c if isinstance(c, dict) else c.to_dict() for c in meta["codecs"]]
    if any(c["name"] == "sharding_indexed" for c in codecs):
        raise NotImplementedError("permuting a sharded array's axes")

    chunks = manifest_chunk_shape(array.metadata)
    new_chunks = [chunks[p] for p in perm]
    # A transpose codec's order lists, for each stored axis, the array axis it
    # holds. Axes the chunk stores in sequence are new axes inverse[stored[k]].
    stored = codecs[0]["configuration"]["order"] if codecs[0]["name"] == "transpose" else range(array.ndim)
    inverse = np.argsort(perm)
    held = [int(inverse[k]) for k in stored]
    if codecs[0]["name"] == "transpose":
        codecs = codecs[1:]
    long = [a for a in held if new_chunks[a] > 1]
    if long != sorted(long):
        codecs = [{"name": "transpose", "configuration": {"order": held}}, *codecs]

    m = array.manifest
    manifest = ChunkManifest.from_arrays(
        paths=np.transpose(m._paths, perm), offsets=np.transpose(m._offsets, perm),
        lengths=np.transpose(m._lengths, perm), validate_paths=False,
        inlined={tuple(k[p] for p in perm): v for k, v in m._inlined.items()} or None)
    new_names = tuple(names[p] for p in perm)
    attrs = dict(meta.get("attributes") or {})
    meta |= {"shape": [array.shape[p] for p in perm], "codecs": codecs,
             "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": new_chunks}},
             "dimension_names": None if array.metadata.dimension_names is None else new_names,
             "attributes": _ordered(attrs, new_names)}
    return ManifestArray(metadata=ArrayV3Metadata.from_dict(meta), chunkmanifest=manifest)


def _common(arrays: Sequence[ManifestArray]) -> tuple[tuple, dict]:
    """The dimension names and attributes, less ``source_url``, that every one of ``arrays`` shares."""
    if not arrays:
        raise ValueError("nothing to combine")
    first = arrays[0].metadata
    names = tuple(first.dimension_names or (None,) * arrays[0].ndim)
    attrs = {k: v for k, v in (first.attributes or {}).items() if k != "source_url"}
    for i, other in enumerate(arrays[1:], 1):
        theirs = tuple(other.metadata.dimension_names or (None,) * other.ndim)
        if theirs != names:
            raise ValueError(f"array {i} has dimensions {theirs}, array 0 has {names}")
        mine = {k: v for k, v in (other.metadata.attributes or {}).items() if k != "source_url"}
        if differ := sorted(k for k in attrs.keys() | mine.keys() if attrs.get(k) != mine.get(k)):
            detail = "; ".join(f"{k}: {attrs.get(k)!r} vs {mine.get(k)!r}" for k in differ)
            raise ValueError(f"array {i}'s attributes differ from array 0's ({detail})")
    return names, attrs


def _replaced(array: ManifestArray, *, names: Sequence[str | None],
              attributes: dict) -> ManifestArray:
    """``array`` with ``names``, ``attributes`` and the ``source_url`` its own manifest gives."""
    _check_per_dimension(attributes, names)
    attrs = _ordered(attributes, names)
    if sources := source_urls(array.manifest):
        attrs["source_url"] = sources
    meta = array.metadata.to_dict() | {"dimension_names": tuple(names), "attributes": attrs}
    return ManifestArray(metadata=ArrayV3Metadata.from_dict(meta), chunkmanifest=array.manifest)


def _ordered(attrs: dict, names: Sequence[str | None]) -> dict:
    """``attrs`` with each per-dimension mapping's keys in axis order, so it reads like the array."""
    out = dict(attrs)
    for key in _PER_DIMENSION:
        if isinstance(value := out.get(key), dict):
            out[key] = {n: value[n] for n in names if n in value}
    return out
