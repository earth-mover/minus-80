"""Chunk references for an uncompressed, C-ordered array laid out at a regular stride.

A flat array has no internal chunk index. Given its shape, dtype and the byte
offset where its data starts, all read from the file's header or sidecar by the
format's own library, each chunk's offset is arithmetic. The array is chunked
along its outermost axis, so each chunk is one contiguous slab: one slice, one
frame or one particle. Along any inner axis a chunk would be scattered through
the file whenever an outer axis is longer than one.

The slabs need not be adjacent. Many acquisition formats interleave a per-record
header before each slab (a Heidelberg ``.vol`` B-scan header, a Bioptigen ``.OCT``
frame header), which leaves the pixel blocks at a constant stride larger than the slab
itself. ``stride`` covers that: the slab bytes are still contiguous and uncompressed,
only their spacing differs, so the manifest stays arithmetic.

Example (MRC volume, header parsed by ``mrcfile``)::

    import mrcfile
    with mrcfile.open(local_path, header_only=True) as m:
        shape = (int(m.header.nz), int(m.header.ny), int(m.header.nx))
        dtype = mrcfile.utils.data_dtype_from_header(m.header)
        data_offset = 1024 + int(m.header.nsymbt)  # base header + extended header
    marr = flat_manifest_array(url, shape=shape, dtype=dtype.newbyteorder("="),
                               data_offset=data_offset,
                               endian="big" if dtype.byteorder == ">" else "little",
                               dimension_names=("z", "y", "x"))
"""
from __future__ import annotations

import math

import numpy as np
from virtualizarr.manifests import ManifestArray

from ..manifest import manifest_array


def flat_manifest_array(
    url: str,
    *,
    shape: tuple[int, ...],
    dtype,
    data_offset: int,
    # No default: a wrong guess at byte order silently byte-swaps every value,
    # so it has to come from the header.
    endian: str,
    stride: int | None = None,
    rows_per_chunk: int = 1,
    dimension_names: tuple[str, ...] | None = None,
    attributes: dict | None = None,
) -> ManifestArray:
    """Virtualize a flat C-ordered array as one Zarr array chunked along its first axis.

    ``data_offset`` is the byte position in the file where element ``[0, 0, ...]``
    begins. ``endian`` (``"little"``/``"big"``) comes from the file header.

    ``stride`` is the byte distance between the starts of successive slabs. It defaults to the slab size (a fully contiguous array); pass a
    larger value for a format that writes a per-record header before each slab, and
    keep ``data_offset`` pointing at the first slab's *pixels*, past that header.

    ``rows_per_chunk`` puts that many consecutive slabs in each chunk, for a
    long 1-D array such as a table column, where one element per chunk would
    give millions of chunks. It needs contiguous slabs. When the rows do not
    divide evenly, the last chunk would be shorter than Zarr's chunk shape and
    could not be decoded, so it is left missing: write it natively from the
    byte range :func:`flat_tail` returns.
    """
    dtype = np.dtype(dtype)
    shape = tuple(int(s) for s in shape)
    itemsize = dtype.itemsize

    n = shape[0]
    rows = int(rows_per_chunk)
    chunk_shape = (rows, *shape[1:])

    # math.prod, not np.prod: np.prod overflows a fixed-width int on a huge slab
    # and corrupts every offset. math.prod([]) == 1 covers the 1-D case.
    slab_bytes = math.prod(shape[1:]) * itemsize
    if stride is None:
        stride = slab_bytes
    elif stride < slab_bytes:
        raise ValueError(
            f"stride ({stride} B) is smaller than one slab ({slab_bytes} B): "
            "successive chunks would overlap."
        )

    if rows > 1 and stride != slab_bytes:
        raise ValueError("rows_per_chunk > 1 needs contiguous slabs (no stride)")

    count = math.ceil(n / rows)
    grid = (count,) + (1,) * (len(shape) - 1)
    offsets = (
        np.uint64(data_offset) + np.arange(count, dtype=np.uint64) * np.uint64(stride * rows)
    ).reshape(grid)
    lengths = np.full(grid, slab_bytes * rows, dtype=np.uint64)
    if n % rows:
        lengths.reshape(-1)[-1] = 0
    return manifest_array(
        url, offsets, lengths, shape=shape, chunk_shape=chunk_shape, dtype=dtype,
        codecs=[{"name": "bytes", "configuration": {"endian": endian}}],
        dimension_names=dimension_names, attributes=attributes)


def flat_tail(
    *, shape: tuple[int, ...], dtype, data_offset: int, rows_per_chunk: int
) -> tuple[int, int, int] | None:
    """The rows and bytes of the short last chunk :func:`flat_manifest_array` leaves missing.

    Returns ``(first_row, start_byte, end_byte)``, or None when the rows divide
    evenly. Read those bytes and assign them to ``array[first_row:]``.
    """
    n, rows = int(shape[0]), int(rows_per_chunk)
    if n % rows == 0:
        return None
    slab_bytes = math.prod(int(s) for s in shape[1:]) * np.dtype(dtype).itemsize
    first = n - n % rows
    return first, data_offset + first * slab_bytes, data_offset + n * slab_bytes
