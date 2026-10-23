"""Generate tiny uncompressed DICOM microscopy instances for offline tests.

Not a faithful WSI writer — just enough valid DICOM (explicit-VR little-endian,
single-frame, single-sample uint16, an OpticalPathSequence naming the marker) that
:func:`minus80.parsers.dicom.parser.channel_metadata` parses it the same way it parses a
real instance, so the roundtrip tests need no network.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def write_multiplex_series(
    out_dir: str | Path,
    markers: list[str],
    *,
    shape: tuple[int, int] = (24, 32),
    seed: int = 0,
) -> tuple[list[str], np.ndarray]:
    """Write one single-frame uint16 instance per marker. Returns the file paths (in
    marker order) and the expected ``(c, y, x)`` stack for assertion."""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    y, x = shape
    paths, planes = [], []

    for i, marker in enumerate(markers):
        plane = rng.integers(0, 4000, size=(y, x), dtype=np.uint16)
        planes.append(plane)

        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.6"  # VL WSI Storage
        fm.MediaStorageSOPInstanceUID = generate_uid()
        fm.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = Dataset()
        ds.file_meta = fm
        ds.SOPClassUID = fm.MediaStorageSOPClassUID
        ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
        ds.Rows, ds.Columns = y, x
        ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = y, x
        ds.NumberOfFrames = 1
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0

        op = Dataset()
        op.OpticalPathIdentifier = str(i + 1)
        op.OpticalPathDescription = marker
        ds.OpticalPathSequence = [op]

        ds.PixelData = plane.tobytes()

        path = out / f"channel_{i:02d}.dcm"
        # Endianness/VR are inferred from the transfer syntax in file_meta.
        pydicom.dcmwrite(path, ds, enforce_file_format=True)
        paths.append(str(path))

    return paths, np.stack(planes)


def write_volume_series(
    out_dir: str | Path,
    z_positions: list[float | None],
    *,
    shape: tuple[int, int] = (6, 8),
    seed: int = 0,
) -> tuple[list[str], np.ndarray]:
    """Write one single-frame int16 CT slice per entry of ``z_positions``.

    Slices are written to disk in reverse order, so file order never matches
    slice order. A ``None`` position leaves ``ImagePositionPatient`` out; each
    slice's ``InstanceNumber`` is its index in ``z_positions``, plus one.
    Returns the file paths and the ``(z, y, x)`` stack in ``z_positions`` order.
    """
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    y, x = shape
    planes = [rng.integers(-1024, 3000, size=(y, x), dtype=np.int16) for _ in z_positions]
    paths = []
    for i in reversed(range(len(z_positions))):
        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage
        fm.MediaStorageSOPInstanceUID = generate_uid()
        fm.TransferSyntaxUID = ExplicitVRLittleEndian

        ds = Dataset()
        ds.file_meta = fm
        ds.SOPClassUID = fm.MediaStorageSOPClassUID
        ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
        ds.InstanceNumber = i + 1
        if z_positions[i] is not None:
            ds.ImagePositionPatient = [0.0, 0.0, z_positions[i]]
        ds.Rows, ds.Columns = y, x
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1
        ds.PixelData = planes[i].tobytes()

        path = out / f"slice_{len(paths):02d}.dcm"
        pydicom.dcmwrite(path, ds, enforce_file_format=True)
        paths.append(str(path))
    return paths, np.stack(planes)


def write_wsi_pyramid_series(
    out_dir: str | Path,
    levels: list[tuple[int, int]],
    *,
    tile: int = 256,
    seed: int = 0,
) -> tuple[list[str], list[np.ndarray]]:
    """Write one uncompressed tiled RGB VL-WSM instance per pyramid level.

    ``levels`` is a list of ``(rows, cols)`` total-pixel-matrix sizes (largest first).
    Each level is tiled TILED_FULL into ``tile``x``tile`` frames (edge tiles zero-padded to
    a full tile, as DICOM requires). Uncompressed, so the roundtrip is byte-exact. Returns
    the file paths and the expected assembled ``(y, x, 3)`` image per level."""
    import math

    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    paths, expected = [], []

    for li, (Y, X) in enumerate(levels):
        nR, nC = math.ceil(Y / tile), math.ceil(X / tile)
        img = rng.integers(0, 256, size=(Y, X, 3), dtype=np.uint8)
        expected.append(img)
        # pack tiles in TILED_FULL raster order, each padded to a full tile
        frames = np.zeros((nR * nC, tile, tile, 3), dtype=np.uint8)
        for r in range(nR):
            for c in range(nC):
                blk = img[r * tile : r * tile + tile, c * tile : c * tile + tile]
                frames[r * nC + c, : blk.shape[0], : blk.shape[1]] = blk

        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.6"
        fm.MediaStorageSOPInstanceUID = generate_uid()
        fm.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = Dataset()
        ds.file_meta = fm
        ds.SOPClassUID = fm.MediaStorageSOPClassUID
        ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
        ds.ImageType = ["DERIVED", "PRIMARY", "VOLUME", "NONE"]
        ds.Rows, ds.Columns = tile, tile
        ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = Y, X
        ds.NumberOfFrames = nR * nC
        ds.SamplesPerPixel = 3
        ds.PlanarConfiguration = 0
        ds.PhotometricInterpretation = "RGB"
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.DimensionOrganizationType = "TILED_FULL"
        ds.PixelData = frames.tobytes()
        path = out / f"level_{li}.dcm"
        pydicom.dcmwrite(path, ds, enforce_file_format=True)
        paths.append(str(path))

    return paths, expected
