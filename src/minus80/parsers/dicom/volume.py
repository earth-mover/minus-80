"""Virtualize a radiology volume (CT/MR/PET) from a DICOM series into OME-Zarr.

A classic radiology Series is many single-slice DICOM instances -- one 2-D image each --
that stack along the through-plane axis into a ``(z, y, x)`` volume. This references each
slice's native PixelData bytes in place (no copy) and assembles them into one array-native
OME-Zarr, ordered by slice position. It is the same primitive as the multiplex case, along
``z`` instead of channel.

Scope: native (uncompressed) transfer syntaxes, single-frame single-sample slices. Compressed
(JPEG/JPEG 2000) and Enhanced multi-frame volumes are not yet supported.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
from minus80.core import as_url
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.manifests.utils import create_v3_array_metadata

from .parser import NATIVE_TRANSFER_SYNTAXES, native_pixeldata_byte_range


def slice_metadata(path: str | Path) -> dict:
    """BUY: read one slice's geometry with ``pydicom`` and locate its native PixelData."""
    import pydicom

    ds = pydicom.dcmread(path, stop_before_pixels=True)
    if str(ds.file_meta.TransferSyntaxUID) not in NATIVE_TRANSFER_SYNTAXES:
        raise ValueError(
            f"{Path(path).name}: {ds.file_meta.TransferSyntaxUID.name} is not a native "
            "(uncompressed) transfer syntax; compressed volumes are out of scope."
        )
    if int(getattr(ds, "NumberOfFrames", 1)) != 1 or int(ds.SamplesPerPixel) != 1:
        raise ValueError(f"{Path(path).name}: expected single-frame single-sample slice.")
    bits = int(ds.BitsAllocated)
    signed = int(getattr(ds, "PixelRepresentation", 0)) == 1
    dtype = {(8, False): np.uint8, (8, True): np.int8,
             (16, False): np.uint16, (16, True): np.int16}.get((bits, signed))
    if dtype is None:
        raise ValueError(f"{Path(path).name}: unsupported bits/representation ({bits}).")
    y, x = int(ds.Rows), int(ds.Columns)
    # Slice order: the through-plane coordinate (ImagePositionPatient z), falling back to
    # InstanceNumber when position is absent.
    ipp = getattr(ds, "ImagePositionPatient", None)
    z = float(ipp[2]) if ipp is not None else None
    expected = y * x * (bits // 8)
    off, length = native_pixeldata_byte_range(path, expected)
    return {
        "shape": (y, x),
        "dtype": np.dtype(dtype),
        "z": z,
        "instance_number": int(getattr(ds, "InstanceNumber", 0)),
        "data_offset": off,
        "length": length,
    }


def volume_array(
    sources: list[str | Path],
    *,
    url_for: Callable[[str | Path], str] = as_url,
) -> tuple[ManifestArray, dict]:
    """Assemble single-slice DICOM instances into one ``(z, y, x)`` ManifestArray.

    Slices are ordered by through-plane position (``ImagePositionPatient`` z, else
    ``InstanceNumber``); each becomes one ``(1, y, x)`` chunk referencing its slice's
    PixelData. Returns the array and geometry attrs (ordered z positions, spacing).
    """
    metas = [(slice_metadata(s), s) for s in sources]
    if len({m["shape"] for m, _ in metas}) != 1:
        raise ValueError("slices are not co-sized (differing (y, x) shapes).")
    if len({m["dtype"] for m, _ in metas}) != 1:
        raise ValueError("slices have differing dtypes.")
    have_z = all(m["z"] is not None for m, _ in metas)
    metas.sort(key=lambda it: it[0]["z"] if have_z else it[0]["instance_number"])

    (y, x), dtype = metas[0][0]["shape"], metas[0][0]["dtype"]
    z_positions = [m["z"] for m, _ in metas]
    spacing = None
    if have_z and len(z_positions) > 1:
        spacing = float(np.median(np.diff(z_positions)))
    zc = len(metas)
    grid = (zc, 1, 1)
    geometry = {"z_positions": z_positions, "z_spacing": spacing}
    metadata = create_v3_array_metadata(
        shape=(zc, y, x),
        data_type=dtype,
        chunk_shape=(1, y, x),
        fill_value=0,
        codecs=[{"name": "bytes", "configuration": {"endian": "little"}}],
        dimension_names=("z", "y", "x"),
        attributes=dict(geometry),
    )
    manifest = ChunkManifest.from_arrays(
        paths=np.array([url_for(s) for _, s in metas], dtype=np.dtypes.StringDType()).reshape(grid),
        offsets=np.array([m["data_offset"] for m, _ in metas], dtype=np.uint64).reshape(grid),
        lengths=np.array([m["length"] for m, _ in metas], dtype=np.uint64).reshape(grid),
    )
    return ManifestArray(metadata=metadata, chunkmanifest=manifest), geometry
