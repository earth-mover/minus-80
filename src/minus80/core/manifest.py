"""Virtual arrays whose chunks are a source's own stored units.

Every parser ends the same way: a grid of ``(path, offset, length)``, one entry
per tile, strip, frame or member, restated as a VirtualiZarr ``ManifestArray``.
:func:`manifest_array` is that step, so the rules for it (what a missing chunk
is, what attributes may hold, which file the array names as its source) are
applied in one place.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
import numpy.typing as npt
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.manifests.manifest import MISSING_CHUNK_PATH
from virtualizarr.manifests.utils import create_v3_array_metadata

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
    dims = set(dimension_names or ())
    for key in _PER_DIMENSION:
        if stray := set(attrs.get(key) or {}) - dims:
            raise ValueError(f"{key} names {sorted(stray)}, which are not dimensions of "
                             f"{tuple(dimension_names or ())}")
    for name, axis in (attrs.get("dimensions") or {}).items():
        if not isinstance(axis, dict) or not isinstance(axis.get("type"), str):
            raise TypeError(f"dimensions[{name!r}] is {axis!r}; each axis needs a 'type' string")
    attrs.pop("source_url", None)
    if sources := source_urls(manifest):
        attrs["source_url"] = sources
    metadata = create_v3_array_metadata(
        shape=shape, data_type=np.dtype(dtype), chunk_shape=chunk_shape,
        fill_value=fill_value, codecs=[dict(c) for c in codecs],
        dimension_names=None if dimension_names is None else tuple(dimension_names),
        attributes=attrs)
    return ManifestArray(metadata=metadata, chunkmanifest=manifest)


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
