"""Read DICOM headers with pydicom and locate PixelData; assemble multiplex channels.

Every function here reads only the header: ``dcmread(..., stop_before_pixels=True)``
parses each element up to PixelData and leaves the file positioned at its tag.
The one thing no DICOM library exposes is a file offset (readers decode, they do
not locate), so the PixelData element header is read here to step to its value.
"""
from __future__ import annotations

import math
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO

import numpy as np
from minus80.core import as_url
from minus80.core.manifest import manifest_array
from minus80.core.registry import file_path
from virtualizarr.manifests import ManifestArray

Source = str | os.PathLike | BinaryIO

PIXEL_DATA_TAG = b"\xe0\x7f\x10\x00"  # (7FE0,0010), little-endian
# Explicit VR elements with these VRs have a 12-byte header; all others 8.
_LONG_VR = {b"OB", b"OW", b"OF", b"OD", b"OL", b"OV", b"UN"}
_LITTLE_ENDIAN_BYTES = [{"name": "bytes", "configuration": {"endian": "little"}}]


def _native_syntaxes() -> frozenset[str]:
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

    return frozenset({ExplicitVRLittleEndian, ImplicitVRLittleEndian})


@dataclass
class _Opened:
    file: BinaryIO
    #: The URL the manifest records; None only for a file object given no ``url``.
    url: str | None
    #: The remote URL the file object reads, so a caller can fetch a local copy.
    remote: str | None
    name: str


@contextmanager
def _opened(source: Source, url: str | None = None, *, need_url: bool = True) -> Iterator[_Opened]:
    """``source`` as a seekable file object, with the URL a manifest should record.

    A path or ``file://`` URL is opened locally; any other URL is read through
    :func:`minus80.core.remote.reader`. A file object is used as given, from its
    start, and is not closed; it states no address, so building a manifest from
    one requires ``url``.
    """
    if hasattr(source, "read"):
        if need_url and url is None:
            raise ValueError("a file object source needs url=, the address the manifest records")
        source.seek(0)
        yield _Opened(source, url, None, url or getattr(source, "name", "<file object>"))
        return
    location = as_url(source)
    name = location.rsplit("/", 1)[-1]
    path = file_path(location)
    if path is not None:
        with open(path, "rb") as f:
            yield _Opened(f, url or location, None, name)
    else:
        from minus80.core.remote import reader

        yield _Opened(reader(location), url or location, location, name)


def _aligned(urls: Sequence[str | None] | None, sources: Sequence) -> list[str | None]:
    if urls is None:
        return [None] * len(sources)
    if len(urls) != len(sources):
        raise ValueError(f"{len(urls)} urls for {len(sources)} sources")
    return list(urls)


def _read_header(f: BinaryIO):
    import pydicom

    return pydicom.dcmread(f, stop_before_pixels=True)


def _pixel_data_value(f: BinaryIO, name: str) -> tuple[int, bool]:
    """The file offset of PixelData's value, and whether it is encapsulated.

    ``f`` must sit at the PixelData tag, where :func:`_read_header` leaves it;
    it is left at the value. Locating the element from where pydicom stopped,
    rather than searching for the tag's bytes, matters because those four bytes
    can occur inside an earlier element's value, such as an ICC profile.
    """
    start = f.tell()
    head = f.read(12)
    if head[:4] != PIXEL_DATA_TAG:
        raise ValueError(f"{name}: no little-endian PixelData element after the header")
    if head[4:6] in _LONG_VR:
        header, undefined = 12, head[8:12] == b"\xff\xff\xff\xff"
    else:
        header, undefined = 8, False
    f.seek(start + header)
    return start + header, undefined


def _dtype(ds, name: str) -> np.dtype:
    """The pixel dtype pydicom reads, signed when ``PixelRepresentation`` is 1."""
    from pydicom.pixels.utils import pixel_dtype

    bits = int(ds.BitsAllocated)
    if bits not in (8, 16):
        raise ValueError(f"{name}: {bits} bits allocated; only 8 and 16 are supported")
    return pixel_dtype(ds)


def _pixel_spacing(ds) -> tuple[float, float] | None:
    """``(row, column)`` spacing in millimetres, from the shared functional groups or the top level.

    Multi-frame microscopy states it in
    ``SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0]``; single-frame
    radiology at the top level.
    """
    spacing = None
    for group in ds.get("SharedFunctionalGroupsSequence") or []:
        for measures in group.get("PixelMeasuresSequence") or []:
            spacing = spacing or measures.get("PixelSpacing")
    spacing = spacing or ds.get("PixelSpacing")
    return None if not spacing else (float(spacing[0]), float(spacing[1]))


def _physical(scale: Mapping[str, float | None]) -> dict:
    """``scale`` and ``units`` attributes for the axes whose spacing the file states."""
    stated = {axis: v for axis, v in scale.items() if v is not None}
    return {"scale": stated, "units": {axis: "millimeter" for axis in stated}} if stated else {}


def _common_spacing(spacings: list, what: str) -> tuple[float, float] | None:
    if any(s is None for s in spacings):
        return None
    if not np.allclose(spacings, spacings[0]):
        raise ValueError(f"{what} state different pixel spacings: {sorted(set(spacings))}")
    return spacings[0]


@dataclass
class _Frame:
    """One single-frame, single-sample, uncompressed instance."""

    ds: object
    url: str
    shape: tuple[int, int]
    dtype: np.dtype
    offset: int
    length: int


def _native_frame(source: Source, url: str | None) -> _Frame:
    with _opened(source, url) as src:
        ds = _read_header(src.file)
        xfer = ds.file_meta.TransferSyntaxUID
        if xfer not in _native_syntaxes():
            raise ValueError(f"{src.name}: transfer syntax {xfer.name} is not uncompressed "
                             "little-endian")
        frames, samples = int(ds.get("NumberOfFrames") or 1), int(ds.SamplesPerPixel)
        if frames != 1 or samples != 1:
            raise ValueError(f"{src.name}: expected one frame of one sample, got {frames} "
                             f"frames of {samples}")
        dtype = _dtype(ds, src.name)
        offset, _ = _pixel_data_value(src.file, src.name)
        shape = (int(ds.Rows), int(ds.Columns))
        return _Frame(ds, src.url, shape, dtype, offset, math.prod(shape) * dtype.itemsize)


def _stack(frames: list[_Frame], dims: tuple[str, str, str], attributes: Mapping) -> ManifestArray:
    """Single-frame instances as one array, each frame one chunk along the first axis."""
    if len({f.shape for f in frames}) != 1 or len({f.dtype for f in frames}) != 1:
        raise ValueError(f"instances differ in shape or dtype: "
                         f"{sorted({(f.shape, str(f.dtype)) for f in frames})}")
    grid = (len(frames), 1, 1)
    return manifest_array(
        np.array([f.url for f in frames], dtype=object).reshape(grid),
        np.array([f.offset for f in frames]).reshape(grid),
        np.array([f.length for f in frames]).reshape(grid),
        shape=(len(frames), *frames[0].shape), chunk_shape=(1, *frames[0].shape),
        dtype=frames[0].dtype, codecs=_LITTLE_ENDIAN_BYTES, dimension_names=dims,
        attributes=attributes)


def dicom_json(source: Source) -> dict:
    """The full DICOM metadata of one instance as DICOM JSON (PS3.18), pixel data excluded.

    Binary elements are kept inline as base64 however large, because some are
    metadata a reader needs: a slide's ICC colour profile is about 140 KB.
    """
    with _opened(source, need_url=False) as src:
        ds = _read_header(src.file)
    return ds.to_json_dict(bulk_data_threshold=math.inf, suppress_invalid_tags=True)


def native_pixeldata_byte_range(source: Source, expected_len: int) -> tuple[int, int]:
    """The file offset of an uncompressed PixelData value, and ``expected_len``."""
    with _opened(source, need_url=False) as src:
        _read_header(src.file)
        offset, encapsulated = _pixel_data_value(src.file, src.name)
    if encapsulated:
        raise ValueError(f"{src.name}: PixelData is encapsulated (compressed)")
    return offset, expected_len


def multiplex_array(
    sources: Sequence[Source],
    *,
    urls: Sequence[str | None] | None = None,
    attributes: Mapping | None = None,
) -> ManifestArray:
    """Single-frame microscopy instances, one optical path each, as one ``(c, y, x)`` array.

    Each instance's frame is one ``(1, y, x)`` chunk referencing its PixelData.
    Channels are ordered by ``OpticalPathIdentifier`` (numerically when it is a
    number), because names repeat: a slide can carry two channels labelled
    ``DNA`` from different staining cycles. The array carries
    ``channel_names`` from ``OpticalPathDescription`` and, when every instance
    states it, the pixel spacing as ``scale``/``units`` in millimetres.
    """
    frames = [_native_frame(s, u) for s, u in zip(sources, _aligned(urls, sources))]

    def path_id(frame: _Frame) -> tuple:
        oid = str(frame.ds.OpticalPathSequence[0].OpticalPathIdentifier)
        return (0, int(oid), "") if oid.isdigit() else (1, 0, oid)

    frames.sort(key=path_id)
    names = []
    for frame in frames:
        path = frame.ds.OpticalPathSequence[0]
        names.append(str(path.get("OpticalPathDescription") or f"path{path.OpticalPathIdentifier}"))
    spacing = _common_spacing([_pixel_spacing(f.ds) for f in frames], "channels")
    attrs = {"channel_names": names,
             **_physical(dict(zip("yx", spacing or (None, None))))}
    return _stack(frames, ("c", "y", "x"), {**attrs, **(attributes or {})})
