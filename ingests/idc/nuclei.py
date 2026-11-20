"""The TCGA nuclei layers: the Pan-Cancer-Nuclei-Seg mask and outlines under a slide.

Trial code for phase 2 of the plan (PLAN.md). It reads local copies of the SEG
and ANN instances, and built the three trial TCGA slides PLAN.md describes.
``ingest.py``'s run does not call it yet: phase 2 adds a TCGA selection that
pairs each slide with its SEG and ANN series and calls ``write_mask`` and
``write_nuclei`` after ``write_slide``.
"""

from __future__ import annotations

import math

import numpy as np
import pydicom
from minus80.ome import label_attrs, labels_attrs
from minus80.parsers.dicom import bulk_annotation_groups, dicom_json, native_pixeldata_byte_range
from minus80.parsers.dicom.ann import CHUNK

from plumbing import BLOSC, Writer, local

ANALYSIS = "pan_cancer_nuclei_seg_dicom"


# The nuclear mask
#
# The SEG is BINARY with one segment, uncompressed, 1 bit per pixel, packed
# least significant bit first, in frames on the slide's own tile grid: one
# instance per slide level, the same total pixel matrix and the same tile
# size, plus one at the thumbnail's size. A frame of 240 x 240 bits is 7,200
# whole bytes, so each frame could be referenced in place, but no Zarr codec
# in zarr-python or zarr-java unpacks bits in that order. The mask is
# therefore written natively as uint8 0/1, one chunk per tile, blosc zstd with
# bit shuffling; an all-background tile is not stored.

def write_mask(w: Writer, source, s: dict, group: str, slide: dict) -> dict:
    kinds = {}
    for url, _, _ in source.objects(s["seg"]["crdc_series_uuid"]):
        path = local(url)
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        kinds[(int(ds.TotalPixelMatrixRows), int(ds.TotalPixelMatrixColumns))] = (url, path, ds)
    by_shape = {tuple(shape[1:]): k for k, shape in enumerate(slide["shapes"])}
    missing = [k for shape, k in by_shape.items() if shape not in kinds]
    if missing:
        raise ValueError(f"{s['slide']}: no SEG instance for image levels {missing}")
    label = f"{group}/image/labels/nuclei"
    total = 0
    for shape, k in sorted(by_shape.items(), key=lambda t: t[1]):
        url, path, ds = kinds[shape]
        if (str(ds.SegmentationType), int(ds.BitsAllocated), str(ds.DimensionOrganizationType)) != \
                ("BINARY", 1, "TILED_FULL") or len(ds.SegmentSequence) != 1:
            raise ValueError(f"{s['slide']}: SEG level {k} is not one binary TILED_FULL segment")
        ty, tx = int(ds.Rows), int(ds.Columns)
        if (ty, tx) != tuple(slide["tile"]):
            raise ValueError(f"{s['slide']}: SEG tile {ty}x{tx}, image tile {slide['tile']}")
        n = int(ds.NumberOfFrames)
        rows, cols = math.ceil(shape[0] / ty), math.ceil(shape[1] / tx)
        if n != rows * cols:
            raise ValueError(f"{s['slide']}: SEG level {k} has {n} frames for a {rows}x{cols} grid")
        frame_bytes = ty * tx // 8
        offset, _ = native_pixeldata_byte_range(path, n * frame_bytes)
        with open(path, "rb") as f:
            f.seek(offset)
            packed = np.frombuffer(f.read(n * frame_bytes), np.uint8)
        frames = np.unpackbits(packed, bitorder="little").reshape(rows, cols, ty, tx)
        full = frames.transpose(0, 2, 1, 3).reshape(rows * ty, cols * tx)[:shape[0], :shape[1]]
        w.native(f"{label}/{k}", full, dims=("y", "x"), chunks=(ty, tx), compressors=BLOSC)
        total += rows * cols
    seg_ds = next(iter(kinds.values()))[2]
    segment = seg_ds.SegmentSequence[0]
    ome = label_attrs(name="nuclei", dims=("y", "x"), level_shapes=[sh[1:] for sh in slide["shapes"]],
                      scale={"y": slide["spacing"][0][0], "x": slide["spacing"][0][1]},
                      translation={"y": 0, "x": 0}, units={"y": "micrometer", "x": "micrometer"},
                      properties=[{"label-value": 1, "SegmentLabel": str(segment.SegmentLabel),
                                   "SegmentedPropertyType": str(segment.SegmentedPropertyTypeCodeSequence[0].CodeMeaning)}])
    w.group(label, {
        "ome": ome,
        "description": ("Pan-Cancer-Nuclei-Seg nuclear mask: 1 inside a nucleus, 0 elsewhere. Each level is "
                        "the SEG instance at that image level's size, unpacked from 1 bit per pixel."),
        "source_url": sorted(u for u, _, _ in kinds.values()),
        "idc": {"SeriesInstanceUID": s["seg"]["SeriesInstanceUID"], "license": s["seg"]["license_short_name"],
                "source_DOI": s["seg"]["source_DOI"], "analysis_result_id": ANALYSIS},
        "dicom": w.document(f"{group}/dicom/seg", dicom_json(next(iter(kinds.values()))[1])),
    })
    w.group(f"{group}/image/labels", {"ome": labels_attrs(["nuclei"])})
    return {"levels": len(by_shape), "tiles": total, "unused_seg_instances": len(kinds) - len(by_shape)}


# The nuclear outlines
#
# The ANN holds one annotation group, "Nuclei": every nucleus as a POLYGON
# (not closed: the last vertex does not repeat the first), as float32 x, y in
# level-0 pixels, the 1-based position of each polygon's first value, and one
# float32 area per nucleus in square micrometers. The three flat arrays are
# referenced in 65,536-row chunks; the last, partial chunk of each is written
# natively, because Zarr decodes only whole chunks. ``offset0`` is derived
# natively: each polygon's first vertex as a 0-based row of ``coords``, then
# the vertex count, the layout CAMELYON's outlines use.

def write_nuclei(w: Writer, source, s: dict, group: str) -> dict:
    (url, _, etag), = source.objects(s["ann"]["crdc_series_uuid"])
    path = local(url)
    groups = bulk_annotation_groups(path, url_for=lambda _p: url)
    if len(groups) != 1 or groups[0].label != "Nuclei" or groups[0].graphic_type != "POLYGON":
        raise ValueError(f"{s['slide']}: unexpected annotation groups {[g.label for g in groups]}")
    g = groups[0]
    data = memoryview(open(path, "rb").read())
    nuclei = f"{group}/nuclei"
    arrays = {"coords": g.coords.array, "point_index": g.point_index.array}
    area, unit = g.measurements["Area"]
    arrays["area"] = area.array
    w.virtual(nuclei, arrays, {
        "description": (f"Pan-Cancer-Nuclei-Seg outlines of {g.count:,} nuclei on slide {s['slide']}. "
                        "coords: polygon vertices (x, y) in level-0 pixels of ../image, unclosed. "
                        "point_index: 1-based position of each polygon's first value in coords as a flat "
                        "array, as DICOM stores it. offset0: each polygon's first vertex row, then the total. "
                        f"area: each nucleus's area in {unit}."),
        "count": g.count,
        "idc": {"SeriesInstanceUID": s["ann"]["SeriesInstanceUID"], "license": s["ann"]["license_short_name"],
                "source_DOI": s["ann"]["source_DOI"], "analysis_result_id": ANALYSIS},
        "dicom": w.document(f"{group}/dicom/ann", with_bulk_uris(dicom_json(path), {
            "00660016": f"{nuclei}/coords", "00660040": f"{nuclei}/point_index", "00660125": f"{nuclei}/area"})),
        "source_url": url,
    }, {url: etag})
    for name, ref in (("coords", g.coords), ("point_index", g.point_index), ("area", area)):
        if ref.tail_length:
            w.root[f"{nuclei}/{name}"][ref.tail_start:] = ref.tail(data)
    point_index = g.point_index.values(data)
    offset0 = np.append((point_index.astype(np.int64) - 1) // 2, g.coords.array.shape[0])
    w.native(f"{nuclei}/offset0", offset0, dims=("boundary",), chunks=(CHUNK,))
    return {"nuclei": g.count, "points": int(g.coords.array.shape[0])}


def with_bulk_uris(document: dict, uris: dict[str, str]) -> dict:
    """``document`` with each listed tag's inline value replaced by a ``BulkDataURI``.

    DICOM JSON (PS3.18 F.2.7) lets a large value be a URI instead of inline
    base64. The ANN's coordinate, index and measurement values are already
    arrays in the store, tens of megabytes per slide, so the header points at
    them instead of repeating them.
    """
    def walk(node):
        if isinstance(node, dict):
            return {k: ({"vr": v["vr"], "BulkDataURI": uris[k]} if k in uris else walk(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(document)
