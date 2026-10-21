"""Flat-binary archetype: an uncompressed, C-ordered array laid out at a regular stride.

The simplest archetype -- there is *no* internal chunk index to parse. Given the
array's shape/dtype and the byte offset where its data starts (both read from the
file's header or sidecar by the format's own library), the chunk manifest is pure
arithmetic. We chunk along one axis (the slowest-varying by default), so each chunk
is one contiguous slab -- ideal for lazy per-slice / per-frame / per-particle reads.

The slabs need not be adjacent. Many acquisition formats interleave a per-record
header before each slab (a Heidelberg ``.vol`` B-scan header, a Bioptigen ``.OCT``
frame header), which leaves the pixel blocks at a constant stride larger than the slab
itself. ``stride`` covers that: the slab bytes are still contiguous and uncompressed,
only their spacing differs, so the manifest stays arithmetic.

Example (MRC volume, header parsed by ``mrcfile``)::

    import mrcfile
    with mrcfile.open(local_path, header_only=True) as m:
        shape = (int(m.header.nz), int(m.header.ny), int(m.header.nx))
        dtype = m.data.dtype                       # from mode
        data_offset = 1024 + int(m.header.nsymbt)  # base header + extended header
    marr = flat_manifest_array(url, shape=shape, dtype=dtype,
                               data_offset=data_offset, endian="little",  # from the header
                               dimension_names=("z", "y", "x"))
"""
from __future__ import annotations

import math

import numpy as np
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.manifests.utils import create_v3_array_metadata


def flat_manifest_array(
    url: str,
    *,
    shape: tuple[int, ...],
    dtype,
    data_offset: int,
    endian: str,  # required, no default: byte order must be read from the header,
                  # never assumed (GOTCHAS.md) — a wrong guess silently byte-swaps.
    stride: int | None = None,
    chunk_along: int = 0,
    dimension_names: tuple[str, ...] | None = None,
    attrs: dict | None = None,
) -> ManifestArray:
    """Virtualize a flat C-ordered array as one Zarr array chunked along one axis.

    ``data_offset`` is the byte position in the file where element ``[0, 0, ...]``
    begins. ``endian`` (``"little"``/``"big"``) comes from the file header.

    ``stride`` is the byte distance between the starts of successive slabs along
    ``chunk_along``. It defaults to the slab size (a fully contiguous array); pass a
    larger value for a format that writes a per-record header before each slab, and
    keep ``data_offset`` pointing at the first slab's *pixels*, past that header.
    """
    dtype = np.dtype(dtype)
    shape = tuple(int(s) for s in shape)
    itemsize = dtype.itemsize

    n = shape[chunk_along]
    chunk_shape = list(shape)
    chunk_shape[chunk_along] = 1
    chunk_shape = tuple(chunk_shape)

    # bytes in one slab (the whole array with the chunked axis pinned to 1);
    # math.prod is exact big-int arithmetic (np.prod overflows a fixed int dtype on a
    # huge slab, corrupting every offset); math.prod([]) == 1 handles the 1-D case.
    other = [shape[i] for i in range(len(shape)) if i != chunk_along]
    slab_bytes = math.prod(other) * itemsize
    if stride is None:
        stride = slab_bytes
    elif stride < slab_bytes:
        raise ValueError(
            f"stride ({stride} B) is smaller than one slab ({slab_bytes} B): "
            "successive chunks would overlap."
        )

    grid = [1] * len(shape)
    grid[chunk_along] = n
    offsets = (
        np.uint64(data_offset) + np.arange(n, dtype=np.uint64) * np.uint64(stride)
    ).reshape(grid)
    lengths = np.full(grid, slab_bytes, dtype=np.uint64)
    paths = np.full(grid, url, dtype=np.dtypes.StringDType())

    metadata = create_v3_array_metadata(
        shape=shape,
        data_type=dtype,
        chunk_shape=chunk_shape,
        fill_value=0,
        codecs=[{"name": "bytes", "configuration": {"endian": endian}}],
        dimension_names=dimension_names,
        attributes=attrs,
    )
    manifest = ChunkManifest.from_arrays(paths=paths, offsets=offsets, lengths=lengths)
    return ManifestArray(metadata=metadata, chunkmanifest=manifest)
