"""Reference the arrays of a DICOM Microscopy Bulk Simple Annotations instance.

An ANN instance stores each annotation group's shapes as flat little-endian
arrays: ``PointCoordinatesData`` (float32 x, y pairs, in total-pixel-matrix
coordinates when ``AnnotationCoordinateType`` is ``2D``),
``LongPrimitivePointIndexList`` (uint32, the 1-based position of each shape's
first *value* in that array) and one ``FloatingPointValues`` array per
measurement. Each is one uncompressed element at a fixed offset, so each
becomes a 1-D or 2-D Zarr array whose chunks are runs of that element's bytes.

pydicom reads the element tree. Nested elements need their file offsets
summed: with ``defer_size``, pydicom reports a deferred element's
``value_tell``, and each enclosing sequence's ``file_tell``, relative to the
start of the sequence element that holds it, so the absolute offset is the sum
along the path from the top-level sequence down. ``tests/parsers/dicom``
checks the bytes at that offset against pydicom's full read.

A run of whole elements does not fill the last chunk when the element's length
is not a multiple of the chunk length, and Zarr's ``bytes`` codec decodes only
full chunks. Each array is returned with that last chunk missing and the byte
range it covers, for the caller to write natively (docs/formats.md, "Write a
padded final chunk natively").
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from minus80.core import as_url
from minus80.core.manifest import manifest_array
from virtualizarr.manifests import ManifestArray

#: Points (or values) per chunk. 65,536 float32 x, y pairs are 512 KiB, small
#: enough that a reader after one region's shapes fetches little beyond them.
CHUNK = 1 << 16


@dataclass
class Referenced:
    """A virtual array whose last chunk is missing, and where its bytes are."""

    array: ManifestArray
    #: Index along axis 0 where the missing last chunk starts.
    tail_start: int
    #: File offset and length of the elements from ``tail_start`` to the end.
    tail_offset: int
    tail_length: int

    def tail(self, data: bytes | memoryview) -> np.ndarray:
        """The rows of the last chunk, from the whole source file's bytes."""
        return self._rows(data, self.tail_offset, self.tail_length)

    def values(self, data: bytes | memoryview) -> np.ndarray:
        """Every row, from the whole source file's bytes."""
        head = self.tail_start * self._row_bytes()
        return self._rows(data, self.tail_offset - head, head + self.tail_length)

    def _row_bytes(self) -> int:
        return np.dtype(self.array.dtype).itemsize * math.prod(self.array.shape[1:])

    def _rows(self, data, offset: int, length: int) -> np.ndarray:
        dtype = np.dtype(self.array.dtype).newbyteorder("<")
        return np.frombuffer(data[offset:offset + length], dtype).reshape(-1, *self.array.shape[1:])


@dataclass
class AnnotationGroup:
    number: int
    label: str
    graphic_type: str
    count: int
    #: ``(n_points, 2)`` float32 x, y.
    coords: Referenced
    #: ``(n_shapes,)`` uint32, 1-based index of each shape's first value in ``coords``.
    point_index: Referenced | None
    #: Measurement name to ``(n_shapes,)`` float32 values, with their unit.
    measurements: dict[str, tuple[Referenced, str]]


def _referenced(url: str, offset: int, length: int, *, dtype, row: tuple[int, ...],
                dims: tuple[str, ...]) -> Referenced:
    dtype = np.dtype(dtype)
    row_bytes = dtype.itemsize * math.prod(row)
    if length % row_bytes:
        raise ValueError(f"{length} bytes is not a whole number of {row_bytes}-byte rows")
    n = length // row_bytes
    grid = math.ceil(n / CHUNK)
    full = n // CHUNK
    chunk_bytes = CHUNK * row_bytes
    offsets = np.zeros((grid, *(1,) * len(row)), np.uint64)
    lengths = np.zeros_like(offsets)
    offsets.reshape(-1)[:full] = offset + np.arange(full, dtype=np.uint64) * chunk_bytes
    lengths.reshape(-1)[:full] = chunk_bytes
    array = manifest_array(
        url, offsets, lengths, shape=(n, *row), chunk_shape=(CHUNK, *row), dtype=dtype,
        codecs=[{"name": "bytes", "configuration": {"endian": "little"}}],
        dimension_names=dims)
    tail_start = full * CHUNK
    return Referenced(array, tail_start, offset + full * chunk_bytes, length - full * chunk_bytes)


def bulk_annotation_groups(
    source: str | Path, *, url_for: Callable[[str | Path], str] = as_url
) -> list[AnnotationGroup]:
    """Each annotation group of one ANN instance, its arrays referenced in place.

    Only ``2D`` coordinates are accepted, the type whose values are pixel
    positions on the slide's total pixel matrix; ``3D`` coordinates are slide
    millimeters and need their own axis description.
    """
    import pydicom

    ds = pydicom.dcmread(source, defer_size=4096)
    if str(ds.Modality) != "ANN":
        raise ValueError(f"{Path(source).name}: Modality {ds.Modality}, not ANN")
    if str(ds.AnnotationCoordinateType) != "2D":
        raise ValueError(f"{Path(source).name}: coordinates are {ds.AnnotationCoordinateType}, not 2D")
    if ds.file_meta.TransferSyntaxUID != "1.2.840.10008.1.2.1":
        raise ValueError(f"{Path(source).name}: transfer syntax {ds.file_meta.TransferSyntaxUID}")
    url = url_for(source)
    sequence = ds["AnnotationGroupSequence"]
    base = sequence.file_tell
    groups = []
    for item in sequence.value:
        def located(holder, keyword, at):
            raw = holder.get_item(keyword)
            return at + raw.value_tell, raw.length

        xy_at, xy_len = located(item, "PointCoordinatesData", base)
        coords = _referenced(url, xy_at, xy_len, dtype="<f4", row=(2,), dims=("point", "xy"))
        point_index = None
        if "LongPrimitivePointIndexList" in item:
            at, n = located(item, "LongPrimitivePointIndexList", base)
            point_index = _referenced(url, at, n, dtype="<u4", row=(), dims=("shape",))
        measurements = {}
        if "MeasurementsSequence" in item:
            outer = item["MeasurementsSequence"]
            for m in outer.value:
                name = str(m.ConceptNameCodeSequence[0].CodeMeaning)
                unit = str(m.MeasurementUnitsCodeSequence[0].CodeValue)
                inner = m["MeasurementValuesSequence"]
                values = inner.value[0]
                if "AnnotationIndexList" in values:
                    raise ValueError(f"{Path(source).name}: measurement {name!r} covers a subset of shapes")
                at, n = located(values, "FloatingPointValues", base + outer.file_tell + inner.file_tell)
                measurements[name] = (_referenced(url, at, n, dtype="<f4", row=(), dims=("shape",)), unit)
        groups.append(AnnotationGroup(
            number=int(item.AnnotationGroupNumber), label=str(item.AnnotationGroupLabel),
            graphic_type=str(item.GraphicType), count=int(item.NumberOfAnnotations),
            coords=coords, point_index=point_index, measurements=measurements))
    return groups
