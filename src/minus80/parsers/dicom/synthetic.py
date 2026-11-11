"""Small synthetic DICOM instances for offline tests.

Each writer produces just enough valid DICOM (explicit-VR little-endian file
meta, the image pixel module, and the elements the parser reads) for the parser
to treat it as it treats a real instance. They are not faithful modality writers.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

import numpy as np

WSI_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.77.1.6"  # VL Whole Slide Microscopy Image
CT_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.2"


def _dataset(sop_class: str, transfer_syntax: str, *, rows: int, columns: int,
             dtype: np.dtype, samples: int = 1, photometric: str | None = None):
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import generate_uid

    dtype = np.dtype(dtype)
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = sop_class
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = transfer_syntax
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.Rows, ds.Columns = rows, columns
    ds.SamplesPerPixel = samples
    if samples > 1:
        ds.PlanarConfiguration = 0
    ds.PhotometricInterpretation = photometric or ("RGB" if samples == 3 else "MONOCHROME2")
    bits = dtype.itemsize * 8
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = bits, bits, bits - 1
    ds.PixelRepresentation = int(dtype.kind == "i")
    return ds


def _shared_spacing(ds, spacing: tuple[float, float]) -> None:
    from pydicom.dataset import Dataset

    measures, group = Dataset(), Dataset()
    measures.PixelSpacing = list(spacing)
    group.PixelMeasuresSequence = [measures]
    ds.SharedFunctionalGroupsSequence = [group]


def _write(ds, path: Path) -> str:
    import pydicom

    path.parent.mkdir(parents=True, exist_ok=True)
    pydicom.dcmwrite(path, ds, enforce_file_format=True)
    return str(path)


def write_multiplex_series(
    out_dir: str | Path,
    markers: list[str],
    *,
    shape: tuple[int, int] = (24, 32),
    spacing: tuple[float, float] | None = (0.0005, 0.0005),
    seed: int = 0,
) -> tuple[list[str], np.ndarray]:
    """Write one single-frame uint16 instance per marker, its spacing in the shared functional groups.

    Returns the file paths (in marker order) and the ``(c, y, x)`` stack.
    """
    from pydicom.dataset import Dataset
    from pydicom.uid import ExplicitVRLittleEndian

    rng = np.random.default_rng(seed)
    planes = rng.integers(0, 4000, size=(len(markers), *shape), dtype=np.uint16)
    paths = []
    for i, marker in enumerate(markers):
        ds = _dataset(WSI_SOP_CLASS, ExplicitVRLittleEndian, rows=shape[0], columns=shape[1],
                      dtype=np.uint16)
        ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = shape
        ds.NumberOfFrames = 1
        op = Dataset()
        op.OpticalPathIdentifier = str(i + 1)
        op.OpticalPathDescription = marker
        ds.OpticalPathSequence = [op]
        if spacing is not None:
            _shared_spacing(ds, spacing)
        ds.PixelData = planes[i].tobytes()
        paths.append(_write(ds, Path(out_dir) / f"channel_{i:02d}.dcm"))
    return paths, planes


def write_volume_series(
    out_dir: str | Path,
    z_positions: list[float | None],
    *,
    shape: tuple[int, int] = (6, 8),
    spacing: tuple[float, float] | None = (0.7, 0.6),
    seed: int = 0,
) -> tuple[list[str], np.ndarray]:
    """Write one single-frame int16 CT slice per entry of ``z_positions``.

    Slices are written to disk in reverse order, so file order never matches
    slice order. A ``None`` position leaves ``ImagePositionPatient`` out; each
    slice's ``InstanceNumber`` is its index in ``z_positions``, plus one.
    ``spacing`` is written as ``PixelSpacing``. Returns the file paths and the
    ``(z, y, x)`` stack in ``z_positions`` order.
    """
    from pydicom.uid import ExplicitVRLittleEndian

    rng = np.random.default_rng(seed)
    planes = rng.integers(-1024, 3000, size=(len(z_positions), *shape), dtype=np.int16)
    paths = []
    for i in reversed(range(len(z_positions))):
        ds = _dataset(CT_SOP_CLASS, ExplicitVRLittleEndian, rows=shape[0], columns=shape[1],
                      dtype=np.int16)
        ds.InstanceNumber = i + 1
        if z_positions[i] is not None:
            ds.ImagePositionPatient = [0.0, 0.0, z_positions[i]]
        if spacing is not None:
            ds.PixelSpacing = list(spacing)
        ds.PixelData = planes[i].tobytes()
        paths.append(_write(ds, Path(out_dir) / f"slice_{len(paths):02d}.dcm"))
    return paths, planes


def _level(ds, rows: int, columns: int, n_frames: int, spacing) -> None:
    ds.ImageType = ["DERIVED", "PRIMARY", "VOLUME", "NONE"]
    ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = rows, columns
    ds.NumberOfFrames = n_frames
    ds.DimensionOrganizationType = "TILED_FULL"
    if spacing is not None:
        _shared_spacing(ds, spacing)


def write_wsi_pyramid_series(
    out_dir: str | Path,
    levels: list[tuple[int, int]],
    *,
    tile: int = 256,
    samples: int = 3,
    dtype: np.dtype = np.uint8,
    spacing: tuple[float, float] = (0.00025, 0.00025),
    optical_path: str | None = None,
    seed: int = 0,
) -> tuple[list[str], list[np.ndarray]]:
    """Write one uncompressed tiled instance per pyramid level, largest first in ``levels``.

    ``levels`` is a list of ``(rows, cols)`` total-pixel-matrix sizes. Each level
    is tiled ``TILED_FULL`` into ``tile`` x ``tile`` frames, edge tiles
    zero-padded as DICOM requires. Level ``i``'s spacing is ``spacing`` times
    level 0's size over its own. ``optical_path`` names each level's single
    ``OpticalPathSequence`` item. Returns the file paths and each level's
    ``(y, x, samples)`` image (``(y, x)`` for one sample).
    """
    from pydicom.dataset import Dataset
    from pydicom.uid import ExplicitVRLittleEndian

    rng = np.random.default_rng(seed)
    info = np.iinfo(dtype)
    paths, expected = [], []
    for li, (Y, X) in enumerate(levels):
        n_rows, n_cols = math.ceil(Y / tile), math.ceil(X / tile)
        img = rng.integers(info.min, info.max, size=(Y, X, samples), dtype=dtype, endpoint=True)
        frames = np.zeros((n_rows, tile, n_cols, tile, samples), dtype=dtype)
        frames.reshape(n_rows * tile, n_cols * tile, samples)[:Y, :X] = img
        level_spacing = (spacing[0] * levels[0][0] / Y, spacing[1] * levels[0][1] / X)
        ds = _dataset(WSI_SOP_CLASS, ExplicitVRLittleEndian, rows=tile, columns=tile,
                      dtype=dtype, samples=samples)
        _level(ds, Y, X, n_rows * n_cols, level_spacing)
        if optical_path is not None:
            op = Dataset()
            op.OpticalPathIdentifier = optical_path
            ds.OpticalPathSequence = [op]
        ds.PixelData = frames.transpose(0, 2, 1, 3, 4).tobytes()
        paths.append(_write(ds, Path(out_dir) / f"level_{optical_path or ''}{li}.dcm"))
        expected.append(img if samples > 1 else img[..., 0])
    return paths, expected


def write_encapsulated_level(
    path: str | Path,
    frames: list[bytes],
    *,
    tile: tuple[int, int],
    columns: int,
    transfer_syntax: str,
    samples: int = 1,
    photometric: str | None = None,
    offset_table: str = "none",
    icc: bytes | None = None,
) -> str:
    """Write a compressed one-row tiled level whose frames are ``frames`` verbatim.

    ``offset_table`` is ``"none"`` (an empty Basic Offset Table), ``"basic"`` or
    ``"extended"``, each built by pydicom. pydicom refuses an empty frame, so
    with ``"none"`` an empty frame is written as an empty Item. ``icc`` puts an
    ICC profile in the ``OpticalPathSequence``.
    """
    from pydicom.dataset import Dataset
    from pydicom.encaps import encapsulate, encapsulate_extended, itemize_fragment

    ds = _dataset(WSI_SOP_CLASS, transfer_syntax, rows=tile[0], columns=tile[1],
                  dtype=np.uint8, samples=samples, photometric=photometric)
    _level(ds, tile[0], columns, len(frames), None)
    if icc is not None:
        optical_path = Dataset()
        optical_path.OpticalPathIdentifier = "1"
        optical_path.ICCProfile = icc
        ds.OpticalPathSequence = [optical_path]
    if offset_table == "extended":
        ds.PixelData, ds.ExtendedOffsetTable, ds.ExtendedOffsetTableLengths = (
            encapsulate_extended(frames))
    elif offset_table == "basic":
        ds.PixelData = encapsulate(frames, has_bot=True)
    elif all(frames):
        ds.PixelData = encapsulate(frames, has_bot=False)
    else:
        ds.PixelData = (itemize_fragment(b"") + b"".join(itemize_fragment(f) for f in frames)
                        + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0))
    ds["PixelData"].VR = "OB"
    ds["PixelData"].is_undefined_length = True
    return _write(ds, Path(path))
