"""Virtualize a tiled DICOM whole-slide image (WSI) level into OME-Zarr.

A WSI pyramid level is one multi-frame DICOM instance whose frames are the tiles of a
`TotalPixelMatrix`, in `TILED_FULL` raster order. This references each tile's *encoded*
bytes in place and exposes the level as a tiled Zarr array; compressed tiles (JPEG 2000 /
JPEG) decode at read time through imagecodecs' own Zarr 3 codecs (``imagecodecs.zarr``,
registered by the ``imagecodecs-zarr`` package) -- so a reader needs those, but no DICOM
reader.

Scope: single-instance `TILED_FULL` levels, JPEG 2000 / JPEG / uncompressed, RGB or
grayscale. Multi-instance concatenations and `TILED_SPARSE` are not yet supported.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from contextlib import contextmanager
from typing import BinaryIO
from pathlib import Path

import numpy as np
from minus80.core import as_url
from minus80.core.manifest import channel_last_transpose, manifest_array
from virtualizarr.manifests import ManifestArray

from .parser import PIXEL_DATA_TAG, native_pixeldata_byte_range

_XFER_CODEC = {
    "1.2.840.10008.1.2.4.90": "imagecodecs_jpeg2k",  # JPEG 2000 lossless
    "1.2.840.10008.1.2.4.91": "imagecodecs_jpeg2k",  # JPEG 2000
    "1.2.840.10008.1.2.4.50": "imagecodecs_jpeg",    # JPEG Baseline
    "1.2.840.10008.1.2.4.51": "imagecodecs_jpeg",    # JPEG Extended
}
_NATIVE = {"1.2.840.10008.1.2", "1.2.840.10008.1.2.1"}
ITEM_TAG = b"\xfe\xff\x00\xe0"  # (FFFE,E000), little-endian
# A JPEG 2000 codestream decodes to RGB only when it is RGB or carries the
# multi-component transform, which DICOM labels YBR_ICT or YBR_RCT. IDC's TCGA
# slides also hold JPEG 2000 labeled YBR_FULL_422 whose codestreams have no
# transform (the COD marker's MCT byte is 0): imagecodecs returns Y, Cb and Cr
# as the three channels, and no Zarr codec converts them.
_J2K_DECODES_TO_RGB = {"RGB", "YBR_ICT", "YBR_RCT"}


def encapsulated_frame_ranges(source: str | Path | BinaryIO) -> list[tuple[int, int]]:
    """Byte (offset, length) of each encoded frame in an encapsulated PixelData.

    ``source`` is a path or a seekable binary file, such as
    ``minus80.core.remote.reader`` over a remote object.

    BUY the locating: ``dcmread(..., stop_before_pixels=True)`` parses every
    element up to PixelData and leaves the file positioned at its tag. Each
    fragment's *value* bytes are the codestream a Zarr codec decodes; the offset
    arithmetic is the one thing no reader exposes. Assumes one fragment per frame
    (the WSI norm).

    When the Basic Offset Table is filled in, the frame positions come from it:
    frame *i*'s Item starts ``BOT[i]`` bytes after the first fragment Item, and
    its value runs up to the next frame's Item. Only the last frame's Item header
    is read, for its length. IDC's GTEx slides carry a filled table, so a level
    is indexed from its header, the table and one 8-byte read, wherever the file
    is. An empty table, as in IDC's TCGA slides, leaves no way but walking every
    fragment Item through the whole PixelData with
    ``pydicom.encaps.parse_fragments``, which over a network means reading the
    file.
    """
    import pydicom
    from pydicom.encaps import parse_fragments

    with _binary(source) as f:
        pydicom.dcmread(f, stop_before_pixels=True)
        head = f.read(12)
        if head[:4] != PIXEL_DATA_TAG or head[8:12] != b"\xff\xff\xff\xff":
            raise ValueError(f"{_name(source)}: PixelData is not encapsulated.")
        bot_item = f.read(8)
        if bot_item[:4] != ITEM_TAG:
            raise ValueError(f"{_name(source)}: no Basic Offset Table Item after PixelData.")
        bot_length = int.from_bytes(bot_item[4:8], "little")
        if bot_length:
            bot = np.frombuffer(f.read(bot_length), "<u4").astype(np.int64)
            first = f.tell()
            starts = first + bot
            f.seek(int(starts[-1]))
            last = f.read(8)
            if last[:4] != ITEM_TAG:
                raise ValueError(f"{_name(source)}: the last Basic Offset Table entry is not an Item.")
            ends = np.append(starts[1:], starts[-1] + 8 + int.from_bytes(last[4:8], "little"))
            return [(int(s) + 8, int(e - s) - 8) for s, e in zip(starts, ends)]
        f.seek(-8, 1)
        _, positions = parse_fragments(f)  # absolute file offsets of each Item tag
        ranges: list[tuple[int, int]] = []
        for item in positions[1:]:  # skip index 0 (the Basic Offset Table Item)
            f.seek(item + 4)
            length = int.from_bytes(f.read(4), "little")
            ranges.append((item + 8, length))
    return ranges


@contextmanager
def _binary(source):
    if isinstance(source, (str, Path)):
        with open(source, "rb") as f:
            yield f
    else:
        source.seek(0)
        yield source


def _name(source) -> str:
    return Path(source).name if isinstance(source, (str, Path)) else repr(source)


def _level_metadata(path: str | Path) -> dict:
    import pydicom

    ds = pydicom.dcmread(path, stop_before_pixels=True)
    xfer = str(ds.file_meta.TransferSyntaxUID)
    if str(getattr(ds, "DimensionOrganizationType", "TILED_FULL")) != "TILED_FULL":
        raise ValueError(f"{Path(path).name}: only TILED_FULL is supported.")
    samples = int(ds.SamplesPerPixel)
    bits = int(ds.BitsAllocated)
    if bits != 8 and not (bits == 16 and samples == 1):
        raise ValueError(f"{Path(path).name}: unsupported bits/samples ({bits}/{samples}).")
    photometric = str(ds.PhotometricInterpretation)
    if _XFER_CODEC.get(xfer) == "imagecodecs_jpeg2k" and samples == 3 and photometric not in _J2K_DECODES_TO_RGB:
        raise ValueError(f"{Path(path).name}: JPEG 2000 in {photometric} decodes to YCbCr, not RGB.")
    return {
        "xfer": xfer,
        "total_y": int(ds.TotalPixelMatrixRows),
        "total_x": int(ds.TotalPixelMatrixColumns),
        "tile_y": int(ds.Rows),
        "tile_x": int(ds.Columns),
        "n_frames": int(ds.NumberOfFrames),
        "samples": samples,
        "dtype": np.dtype("uint8" if bits == 8 else "uint16"),
    }


def wsi_level_array(
    path: str | Path, *, url_for: Callable[[str | Path], str] = as_url
) -> ManifestArray:
    """One tiled WSI pyramid level as a virtual ManifestArray (one chunk per tile-frame)."""
    m = _level_metadata(path)
    ty, tx, s = m["tile_y"], m["tile_x"], m["samples"]
    n_rows = math.ceil(m["total_y"] / ty)
    n_cols = math.ceil(m["total_x"] / tx)
    if n_rows * n_cols != m["n_frames"]:
        raise ValueError(
            f"{Path(path).name}: tile grid {n_rows}x{n_cols} != {m['n_frames']} frames."
        )

    xfer = m["xfer"]
    if xfer in _XFER_CODEC:
        ranges = encapsulated_frame_ranges(path)
        codec = {"name": _XFER_CODEC[xfer]}
    elif xfer in _NATIVE:  # contiguous native frames: arithmetic offsets
        frame_bytes = ty * tx * s * m["dtype"].itemsize
        off0, _ = native_pixeldata_byte_range(path, frame_bytes * m["n_frames"])
        ranges = [(off0 + k * frame_bytes, frame_bytes) for k in range(m["n_frames"])]
        codec = None
    else:
        raise ValueError(f"{Path(path).name}: transfer syntax {xfer} out of scope.")
    if len(ranges) != m["n_frames"]:
        raise ValueError(f"{Path(path).name}: {len(ranges)} fragments != {m['n_frames']} frames.")

    Y, X = m["total_y"], m["total_x"]
    # The serializer is the image codec itself for compressed frames (imagecodecs'
    # codecs are array-to-bytes: a JPEG decodes straight to an array), and ``bytes``
    # for native frames.
    serializer = codec if codec is not None else {"name": "bytes", "configuration": {"endian": "little"}}
    if s == 3:
        # RGB tiles are stored pixel-interleaved (y, x, c), but OME-Zarr requires the
        # channel axis before space, so the array is (c, y, x). A transpose codec maps
        # the decoded interleaved layout to channel-first, keeping the array and its
        # downsampled pyramid axis-consistent.
        shape, chunk, grid, dims = (s, Y, X), (s, ty, tx), (1, n_rows, n_cols), ("c", "y", "x")
        codecs = [channel_last_transpose(3), serializer]
    else:
        shape, chunk, grid, dims = (Y, X), (ty, tx), (n_rows, n_cols), ("y", "x")
        codecs = [serializer]

    # Frames are raster-ordered (row-major over the tile grid), so a C-order reshape of
    # the frame-index list onto the grid puts frame r*n_cols+c at cell (…, r, c).
    # An encapsulated frame whose fragment Item is empty has no stored bytes, and
    # manifest_array makes it a missing chunk that reads as fill.
    offsets = np.array([o for o, _ in ranges], dtype=np.uint64).reshape(grid)
    lengths = np.array([ln for _, ln in ranges], dtype=np.uint64).reshape(grid)
    return manifest_array(
        url_for(path), offsets, lengths, shape=shape, chunk_shape=chunk, dtype=m["dtype"],
        codecs=codecs, dimension_names=dims,
        attributes={"tile_size": [ty, tx], "transfer_syntax": xfer})


def pyramid_levels(sources: list[str | Path]) -> list[tuple[int, int, str | Path]]:
    """The series' ``VOLUME`` instances as ``(rows, cols, source)``, largest (base) first.

    A DICOM WSI series carries its pyramid as several instances; overview, thumbnail
    and label instances are not levels of it and are left out.
    """
    import pydicom

    volumes = []
    for s in sources:
        ds = pydicom.dcmread(s, stop_before_pixels=True)
        it = list(getattr(ds, "ImageType", []))
        if len(it) > 2 and it[2] == "VOLUME":
            volumes.append((int(ds.TotalPixelMatrixRows), int(ds.TotalPixelMatrixColumns), s))
    if not volumes:
        raise ValueError("no VOLUME pyramid levels found in the series.")
    volumes.sort(key=lambda t: -(t[0] * t[1]))
    return volumes
