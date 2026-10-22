"""Virtualize uncompressed DICOM microscopy into Icechunk. BUY the parsing; build only references.

DICOM is an interchange/archive format, not a chunked array format: a multiplexed
slide arrives as *many* single-frame objects (one antibody marker each), and a
whole-slide image tiles each pyramid level into frames. Either way the pixels are
already bytes in the file — this maps them to Zarr chunks with no copy and no decode.

We NEVER hand-parse the DICOM element tree; ``pydicom`` reads the geometry (optical
paths, tile/matrix size, transfer syntax). The one low-level thing we compute is the
byte offset+length of the **PixelData value**, because a native (uncompressed) frame
is exactly a Zarr chunk's bytes — the flat-binary archetype, per-frame.

This module covers the multiplex case (N single-frame instances -> one ``(c, y, x)``
array with named channels); ``wsi`` covers tiled multi-frame levels, compressed or not,
and ``volume`` covers radiology slice stacks.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import numpy as np
from minus80.core import as_url
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.manifests.utils import create_v3_array_metadata

PIXEL_DATA_TAG = b"\xe0\x7f\x10\x00"  # (7FE0,0010), little-endian
# Native (uncompressed) transfer syntaxes we can reference directly.
NATIVE_TRANSFER_SYNTAXES = {"1.2.840.10008.1.2", "1.2.840.10008.1.2.1"}


_LONG_VR = {b"OB", b"OW", b"OF", b"OD", b"OL", b"OV", b"UN"}  # explicit-VR 12-byte header


def dicom_json(path: str | Path) -> dict:
    """The full DICOM metadata of one instance as standard DICOM JSON (PS3.18).

    Virtualization references bytes in place, so the metadata that ships with the file
    must ship with the virtual store too — throwing it away would defeat the point. This
    is the whole element tree (patient/study/series, acquisition, optical paths, spacing,
    ICC profiles, …) minus the pixel data, in the standard JSON model, ready to store as Zarr attrs.
    """
    import pydicom

    ds = pydicom.dcmread(path, stop_before_pixels=True)
    # Binary elements are kept inline as base64 however large, because some are
    # metadata a reader needs: a slide's ICC colour profile is ~140 KB. The pixel
    # data itself is already excluded by stop_before_pixels.
    return ds.to_json_dict(bulk_data_threshold=math.inf, suppress_invalid_tags=True)


def native_pixeldata_byte_range(path: str | Path, expected_len: int) -> tuple[int, int]:
    """Byte offset+length of native (uncompressed) PixelData's value.

    No DICOM library exposes the *file* offset of pixel data (readers decode, they don't
    locate bytes), so the offset is the one irreducibly virtualization-specific piece. We
    still lean on pydicom for the locating: ``dcmread(..., stop_before_pixels=True)`` parses
    every element up to PixelData and leaves the file positioned at its tag, so we don't
    scan for it ourselves (PixelData is not always the last element). We then read the one
    standard element header (tag + VR + length) to step over it to the value.
    """
    import pydicom

    with open(path, "rb") as f:
        pydicom.dcmread(f, stop_before_pixels=True)  # positions f at the PixelData element
        start = f.tell()
        head = f.read(12)
    if head[:4] != PIXEL_DATA_TAG:
        raise ValueError(f"{Path(path).name}: not positioned at PixelData (encapsulated?).")
    vr = head[4:6]
    # Explicit VR long form (OB/OW/…): tag(4)+VR(2)+reserved(2)+length(4) = 12-byte header.
    # Explicit VR short form: 8; Implicit VR (no VR field): 8.
    header = 12 if vr in _LONG_VR else 8
    return start + header, expected_len


def channel_metadata(path: str | Path) -> dict:
    """BUY: read one single-frame microscopy instance's layout with ``pydicom``.

    Returns the facts a channel needs: the marker name and optical-path id, the
    (y, x) shape and dtype, and the PixelData byte range. Raises on anything not a
    single-frame uncompressed grayscale channel (compressed / RGB / multi-frame),
    rather than guessing.
    """
    import pydicom

    ds = pydicom.dcmread(path, stop_before_pixels=True)
    if str(ds.file_meta.TransferSyntaxUID) not in NATIVE_TRANSFER_SYNTAXES:
        raise ValueError(
            f"{Path(path).name}: transfer syntax {ds.file_meta.TransferSyntaxUID.name} "
            "is not uncompressed (native); out of scope."
        )
    if int(getattr(ds, "NumberOfFrames", 1)) != 1 or int(ds.SamplesPerPixel) != 1:
        raise ValueError(
            f"{Path(path).name}: expected single-frame single-sample channel "
            f"(got frames={getattr(ds, 'NumberOfFrames', 1)}, spp={ds.SamplesPerPixel})."
        )
    bits = int(ds.BitsAllocated)
    dtype = {8: np.uint8, 16: np.uint16}.get(bits)
    if dtype is None or int(getattr(ds, "PixelRepresentation", 0)) != 0:
        raise ValueError(f"{Path(path).name}: unsupported bits/representation ({bits}).")
    op = ds.OpticalPathSequence[0]
    opt_id = str(op.OpticalPathIdentifier)
    marker = str(getattr(op, "OpticalPathDescription", "") or f"path{opt_id}")
    y = int(getattr(ds, "TotalPixelMatrixRows", ds.Rows))
    x = int(getattr(ds, "TotalPixelMatrixColumns", ds.Columns))
    off, length = native_pixeldata_byte_range(path, y * x * (bits // 8))
    return {
        "optical_path_id": opt_id,
        "marker": marker,
        "shape": (y, x),
        "dtype": np.dtype(dtype),
        "data_offset": off,
        "length": length,
    }


def multiplex_array(
    sources: list[str | Path],
    *,
    url_for: Callable[[str | Path], str] = as_url,
) -> tuple[ManifestArray, list[str]]:
    """Assemble N single-frame DICOM channel instances into one ``(c, y, x)`` ManifestArray.

    Each source is one marker's instance; its single frame becomes one ``(1, y, x)``
    chunk that references that file's PixelData bytes. Channels are ordered by optical
    path id (numeric when possible). ``url_for`` maps a source to the URL the manifest
    references (default: the file's own ``file://`` URL; pass a lambda to reference the
    same bytes on a remote store, such as an archive's public HTTPS objects). Returns the array
    and the ordered marker names.
    """
    metas = [(channel_metadata(s), s) for s in sources]
    if len({m["shape"] for m, _ in metas}) != 1:
        raise ValueError("channels are not co-registered (differing (y, x) shapes).")

    def sort_key(item):
        oid = item[0]["optical_path_id"]
        return (0, int(oid)) if oid.isdigit() else (1, oid)

    metas.sort(key=sort_key)
    (y, x), dtype = metas[0][0]["shape"], metas[0][0]["dtype"]
    c = len(metas)
    grid = (c, 1, 1)
    markers = [m["marker"] for m, _ in metas]
    metadata = create_v3_array_metadata(
        shape=(c, y, x),
        data_type=dtype,
        chunk_shape=(1, y, x),
        fill_value=0,
        codecs=[{"name": "bytes", "configuration": {"endian": "little"}}],
        dimension_names=("c", "y", "x"),
        attributes={"channel_names": markers},
    )
    manifest = ChunkManifest.from_arrays(
        paths=np.array([url_for(s) for _, s in metas], dtype=np.dtypes.StringDType()).reshape(grid),
        offsets=np.array([m["data_offset"] for m, _ in metas], dtype=np.uint64).reshape(grid),
        lengths=np.array([m["length"] for m, _ in metas], dtype=np.uint64).reshape(grid),
    )
    return ManifestArray(metadata=metadata, chunkmanifest=manifest), markers
