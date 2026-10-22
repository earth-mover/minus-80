"""Tiled whole-slide pyramid levels: one multi-frame instance per level, one chunk per frame.

A level's frames are the tiles of its ``TotalPixelMatrix`` in ``TILED_FULL``
raster order, so frame ``r * n_cols + c`` is tile ``(r, c)``. Compressed tiles
keep their encoded bytes and decode at read time through imagecodecs' Zarr 3
codecs; a reader of the store needs those and does not need a DICOM library.
"""
from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from typing import BinaryIO

import numpy as np
from minus80.core.manifest import channel_last_transpose, manifest_array
from virtualizarr.manifests import ManifestArray

from .parser import (
    _LITTLE_ENDIAN_BYTES,
    Source,
    _aligned,
    _dtype,
    _native_syntaxes,
    _opened,
    _Opened,
    _physical,
    _pixel_data_value,
    _pixel_spacing,
    _read_header,
)

_ITEM_TAG = b"\xfe\xff\x00\xe0"  # (FFFE,E000), little-endian


def _codec(xfer: str) -> dict | None:
    """imagecodecs' Zarr 3 codec for a compressed transfer syntax, or None if there is none."""
    from imagecodecs.zarr import Jpeg, Jpeg2k
    from pydicom.uid import JPEG2000, JPEG2000Lossless, JPEGBaseline8Bit, JPEGExtended12Bit

    codec = {JPEG2000: Jpeg2k, JPEG2000Lossless: Jpeg2k,
             JPEGBaseline8Bit: Jpeg, JPEGExtended12Bit: Jpeg}.get(xfer)
    return None if codec is None else codec().to_dict()


def encapsulated_frame_ranges(source: Source) -> list[tuple[int, int]]:
    """The file offset and length of each encoded frame of a compressed instance.

    Each frame must be one fragment, the norm for whole-slide images. The
    offsets come from the Extended Offset Table when the instance has one,
    else from a non-empty Basic Offset Table; only an instance with neither is
    walked fragment by fragment, and a remote one is downloaded first, since a
    walk over range requests is one round trip per frame.
    """
    with _opened(source, need_url=False) as src:
        return _frame_ranges(_read_header(src.file), src)


def _frame_ranges(ds, src: _Opened) -> list[tuple[int, int]]:
    from pydicom.encaps import parse_basic_offsets

    f = src.file
    _, encapsulated = _pixel_data_value(f, src.name)
    if not encapsulated:
        raise ValueError(f"{src.name}: PixelData is not encapsulated")
    basic = parse_basic_offsets(f)
    # Both offset tables count from the first fragment's Item tag, which
    # follows the Basic Offset Table item.
    first = f.tell()
    if "ExtendedOffsetTable" in ds:
        offsets = np.frombuffer(ds.ExtendedOffsetTable, "<u8").astype(np.int64)
        lengths = np.frombuffer(ds.ExtendedOffsetTableLengths, "<u8").astype(np.int64)
        if np.any(offsets[1:] != offsets[:-1] + lengths[:-1] + 8):
            raise ValueError(f"{src.name}: frames in the Extended Offset Table are not one "
                             "fragment each")
    elif basic:
        offsets = np.asarray(basic, dtype=np.int64)
        lengths = np.empty_like(offsets)
        lengths[:-1] = np.diff(offsets) - 8
        lengths[-1] = _item_length(f, first + offsets[-1], src.name)
    else:
        return _walk_fragments(src)
    # A table alone cannot show that each frame is one fragment; a frame split
    # into several would take the later Item headers into its chunk. The first
    # frame's Item is checked against the table.
    if len(offsets) and _item_length(f, first + offsets[0], src.name) != lengths[0]:
        raise ValueError(f"{src.name}: the first frame is not one fragment")
    return [(int(first + o + 8), int(n)) for o, n in zip(offsets, lengths)]


def _item_length(f: BinaryIO, at: int, name: str) -> int:
    f.seek(at)
    head = f.read(8)
    if head[:4] != _ITEM_TAG:
        raise ValueError(f"{name}: no fragment Item at byte {at}")
    return struct.unpack("<I", head[4:])[0]


def _walk_fragments(src: _Opened) -> list[tuple[int, int]]:
    """Every fragment's value range, from pydicom's walk over the encapsulated value."""
    from pydicom.encaps import parse_basic_offsets

    if src.remote is None:
        return _fragment_ranges(src.file)
    from minus80.core.fetch import download_then_index

    with open(download_then_index(src.remote), "rb") as f:
        _read_header(f)
        _pixel_data_value(f, src.name)
        parse_basic_offsets(f)
        return _fragment_ranges(f)


def _fragment_ranges(f: BinaryIO) -> list[tuple[int, int]]:
    from pydicom.encaps import parse_fragments

    _, positions = parse_fragments(f)
    ranges = []
    for item in positions:
        f.seek(item + 4)
        ranges.append((item + 8, int.from_bytes(f.read(4), "little")))
    return ranges


def _level_array(ds, src: _Opened, attributes: Mapping | None) -> ManifestArray:
    name = src.name
    if str(ds.get("DimensionOrganizationType") or "TILED_FULL") != "TILED_FULL":
        raise ValueError(f"{name}: only TILED_FULL levels are supported")
    samples = int(ds.SamplesPerPixel)
    if samples not in (1, 3):
        raise ValueError(f"{name}: {samples} samples per pixel; only 1 and 3 are supported")
    dtype = _dtype(ds, name)
    ty, tx = int(ds.Rows), int(ds.Columns)
    total_y, total_x = int(ds.TotalPixelMatrixRows), int(ds.TotalPixelMatrixColumns)
    n_rows, n_cols = math.ceil(total_y / ty), math.ceil(total_x / tx)
    n_frames = int(ds.NumberOfFrames)
    if n_rows * n_cols != n_frames:
        raise ValueError(f"{name}: a {n_rows}x{n_cols} tile grid for {n_frames} frames")

    xfer = ds.file_meta.TransferSyntaxUID
    if xfer in _native_syntaxes():
        start, _ = _pixel_data_value(src.file, name)
        frame_bytes = ty * tx * samples * dtype.itemsize
        ranges = [(start + k * frame_bytes, frame_bytes) for k in range(n_frames)]
        serializer = _LITTLE_ENDIAN_BYTES[0]
    elif (serializer := _codec(xfer)) is not None:
        ranges = _frame_ranges(ds, src)
    else:
        raise ValueError(f"{name}: transfer syntax {xfer.name} has no Zarr codec here")
    if len(ranges) != n_frames:
        raise ValueError(f"{name}: {len(ranges)} fragments for {n_frames} frames")

    if samples == 3:
        shape, chunks, grid, dims = (3, total_y, total_x), (3, ty, tx), (1, n_rows, n_cols), ("c", "y", "x")
        codecs = [channel_last_transpose(3), serializer]
    else:
        shape, chunks, grid, dims = (total_y, total_x), (ty, tx), (n_rows, n_cols), ("y", "x")
        codecs = [serializer]
    spacing = _pixel_spacing(ds) or (None, None)
    attrs = {"transfer_syntax": str(xfer), **_physical(dict(zip("yx", spacing)))}
    return manifest_array(
        src.url,
        np.array([o for o, _ in ranges], dtype=np.uint64).reshape(grid),
        np.array([n for _, n in ranges], dtype=np.uint64).reshape(grid),
        shape=shape, chunk_shape=chunks, dtype=dtype, codecs=codecs, dimension_names=dims,
        attributes={**attrs, **(attributes or {})})


def wsi_level_array(
    source: Source, *, url: str | None = None, attributes: Mapping | None = None
) -> ManifestArray:
    """One pyramid level as a ``(c, y, x)`` (three samples) or ``(y, x)`` array, one chunk per tile.

    The level's own ``PixelSpacing`` becomes ``scale``/``units`` in millimetres.
    A frame with no stored bytes is a missing chunk that reads as 0.
    """
    with _opened(source, url) as src:
        return _level_array(_read_header(src.file), src, attributes)


def wsi_levels(
    sources: Sequence[Source],
    *,
    urls: Sequence[str | None] | None = None,
    attributes: Mapping | None = None,
) -> dict[str, ManifestArray]:
    """A slide's pyramid, keyed ``"0"`` (largest), ``"1"``, … from the instances of its series.

    Only ``VOLUME`` instances are levels: overview, thumbnail and label images
    in the same series are left out. Each level is built as by
    :func:`wsi_level_array` and states its own spacing; none is derived.
    """
    levels, paths = [], set()
    for source, url in zip(sources, _aligned(urls, sources)):
        with _opened(source, url) as src:
            ds = _read_header(src.file)
            image_type = list(ds.get("ImageType") or [])
            if len(image_type) > 2 and image_type[2] == "VOLUME":
                levels.append(_level_array(ds, src, attributes))
                paths.update(str(p.get("OpticalPathIdentifier"))
                             for p in ds.get("OpticalPathSequence") or [])
    if not levels:
        raise ValueError("no VOLUME instances among the sources")
    # A multiplexed slide stores each channel's pyramid as its own instances
    # in one series; sorting those by size would interleave channels as levels.
    if len(paths) > 1:
        raise ValueError(f"the instances span optical paths {sorted(paths)}; pass one "
                         "optical path's instances")
    levels.sort(key=lambda a: -math.prod(a.shape[-2:]))
    if len({a.shape for a in levels}) != len(levels):
        raise ValueError("two VOLUME instances have the same size")
    return {str(i): level for i, level in enumerate(levels)}
