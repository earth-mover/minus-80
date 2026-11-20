"""Reference IDC whole-slide images, and the Pan-Cancer nuclei results on TCGA, from an OME-Zarr store.

NCI Imaging Data Commons (IDC) publishes slide microscopy as DICOM in the
public bucket ``s3://idc-open-data``: one prefix per series, one object per
instance. A slide is one series, and each of its pyramid levels is one tiled
instance whose frames are the JPEG or JPEG 2000 tiles. The store references
those tiles where they lie, so every slide stays one bucket away from its
pixels.

For TCGA, IDC also publishes the Pan-Cancer-Nuclei-Seg results (Stony Brook)
as two DICOM series per slide:

- a SEG, the binary nuclear mask, one instance per slide level, on the slide's
  own tile grid, 1 bit per pixel;
- an ANN, the outline of every nucleus as a polygon in level-0 pixels, with
  its area.

The store holds, per slide::

    <collection>/<patient>/<slide>/
        image/            OME-Zarr multiscale (c, y, x), every level virtual
            labels/nuclei/  the SEG mask, native, one level per image level   (TCGA)
        thumbnail/        the series' THUMBNAIL instance, virtual
        overview/         the series' OVERVIEW instance (macro photo), virtual (GTEx)
        nuclei/           the ANN polygons: coords and area virtual, offsets native (TCGA)
        dicom/            each instance's DICOM JSON, native bytes

Run::

    uv run --package idc-ingest python ingests/idc/ingest.py REPO --collection gtex SLIDE ...
    uv run --package idc-ingest python ingests/idc/ingest.py REPO --collection tcga SLIDE ...
"""

from __future__ import annotations

import argparse
import math

import numpy as np
import pydicom
from minus80.core.icechunk_io import commit
from minus80.ome import image_attrs, label_attrs, labels_attrs, rgb_omero
from minus80.parsers.dicom import dicom_json, native_pixeldata_byte_range, wsi_level_array
from minus80.parsers.dicom.ann import CHUNK, bulk_annotation_groups

from plumbing import BLOSC, SOURCE, Writer, local, open_repository, say, series_objects

UM = {"c": None, "y": "micrometer", "x": "micrometer"}
ANALYSIS = "pan_cancer_nuclei_seg_dicom"


# Selection
#
# idc-index is IDC's own catalog: one row per series, with the collection,
# patient, license and the bucket prefix. A slide is chosen by its
# ContainerIdentifier, the barcode on the glass. For TCGA a slide is taken
# only with both of its nuclei series, found by the slide series they
# reference, not by patient: a TCGA patient has several slides, and the
# results cover one of them.

def select(collection: str, slides: list[str]) -> list[dict]:
    from idc_index import IDCClient

    client = IDCClient()
    for name in ("sm_index", "ann_index", "seg_index"):
        client.fetch_index(name)
    index, sm = client.index, client.sm_index
    rows = index.merge(sm[["SeriesInstanceUID", "ContainerIdentifier",
                            "primaryAnatomicStructure_CodeMeaning"]], on="SeriesInstanceUID")
    rows = rows[rows.ContainerIdentifier.isin(slides)]
    if collection == "gtex":
        rows = rows[rows.collection_id == "gtex"]
    else:
        rows = rows[rows.collection_id.str.startswith("tcga_")]
    if sorted(rows.ContainerIdentifier) != sorted(set(slides)):
        raise ValueError(f"not found exactly once: {sorted(set(slides) - set(rows.ContainerIdentifier))}")
    results = index[index.analysis_result_id == ANALYSIS]
    ann = client.ann_index.merge(results, on="SeriesInstanceUID")
    seg = client.seg_index.merge(results, on="SeriesInstanceUID")
    picked = []
    for _, r in rows.iterrows():
        slide = {"collection": r.collection_id, "patient": r.PatientID, "slide": r.ContainerIdentifier,
                 "series": r.SeriesInstanceUID, "crdc_series_uuid": r.crdc_series_uuid,
                 "license": r.license_short_name, "doi": r.source_DOI,
                 "tissue": r.primaryAnatomicStructure_CodeMeaning}
        if collection == "tcga":
            a = ann[ann.referenced_SeriesInstanceUID == r.SeriesInstanceUID]
            s = seg[seg.segmented_SeriesInstanceUID == r.SeriesInstanceUID]
            if len(a) != 1 or len(s) != 1:
                raise ValueError(f"{r.ContainerIdentifier}: {len(a)} ANN and {len(s)} SEG series")
            slide["ann"] = a.iloc[0][["SeriesInstanceUID", "crdc_series_uuid", "license_short_name",
                                      "source_DOI"]].to_dict()
            slide["seg"] = s.iloc[0][["SeriesInstanceUID", "crdc_series_uuid", "license_short_name",
                                      "source_DOI"]].to_dict()
        picked.append(slide)
    return picked


# The slide
#
# A slide series holds one VOLUME instance per pyramid level, a THUMBNAIL and,
# for GTEx, an OVERVIEW photograph of the glass slide. Every level shares the
# total pixel matrix's top-left corner, so the levels' translations make the
# corners coincide. The pixel size is the shared functional group's
# PixelSpacing, stated in millimeters; each level's own stated value is kept
# beside the shape-derived scale OME-Zarr receives.

def instances(objects: list[tuple[str, int]]) -> dict[str, list[tuple[str, str, pydicom.Dataset]]]:
    kinds: dict[str, list] = {}
    for url, _ in objects:
        path = local(url)
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        kinds.setdefault(str(ds.ImageType[2]), []).append((url, path, ds))
    return kinds


def pixel_spacing_um(ds: pydicom.Dataset) -> list[float]:
    measures = ds.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0]
    return [float(v) * 1000 for v in measures.PixelSpacing]


def write_slide(w: Writer, s: dict, group: str) -> dict:
    kinds = instances(series_objects(s["crdc_series_uuid"]))
    levels = sorted(kinds["VOLUME"], key=lambda t: -int(t[2].TotalPixelMatrixRows))
    arrays = {str(k): wsi_level_array(path, url_for=lambda _p, u=url: u)
              for k, (url, path, _) in enumerate(levels)}
    shapes = [a.shape for a in arrays.values()]
    spacing = [pixel_spacing_um(ds) for _, _, ds in levels]
    tile = arrays["0"].metadata.chunk_grid.chunk_shape[1:]
    codec = arrays["0"].metadata.codecs[-1].to_dict()["name"]
    headers = {}
    for k, (url, path, ds) in enumerate(levels):
        headers[str(k)] = w.document(f"{group}/dicom/image_{k}", dicom_json(path))
    w.virtual(f"{group}/image", arrays, {
        "ome": image_attrs(name=s["slide"], dims=("c", "y", "x"), level_shapes=shapes,
                           scale={"c": 1, "y": spacing[0][0], "x": spacing[0][1]},
                           translation={"c": 0, "y": 0, "x": 0}, units=UM, omero=rgb_omero(),
                           description="The scanner's pyramid as IDC's DICOM conversion keeps it: "
                                       "each level is one tiled instance."),
        "description": (f"Slide {s['slide']}, RGB, H&E. Each chunk is one {tile[0]} x {tile[1]} "
                        f"{codec.removeprefix('imagecodecs_')} tile of the level's DICOM instance."),
        "stated_pixel_spacing_um": spacing,
        "dicom": headers,
    })
    extras = []
    for kind, name in (("THUMBNAIL", "thumbnail"), ("OVERVIEW", "overview")):
        for url, path, ds in kinds.get(kind, []):
            marr = wsi_level_array(path, url_for=lambda _p, u=url: u)
            if kind == "THUMBNAIL":
                um = pixel_spacing_um(ds)
                ome = image_attrs(name=f"{s['slide']} {name}", dims=("c", "y", "x"), level_shapes=[marr.shape],
                                  scale={"c": 1, "y": um[0], "x": um[1]}, units=UM, omero=rgb_omero())
                about = "A reduced copy of the whole slide, one uncompressed frame."
            else:
                ome = image_attrs(name=f"{s['slide']} {name}", dims=("c", "y", "x"), level_shapes=[marr.shape],
                                  scale={"c": 1, "y": 1, "x": 1}, omero=rgb_omero())
                about = ("A photograph of the whole glass slide, one uncompressed frame. It shares no frame "
                         "with the scan, so its axes carry no unit.")
            w.virtual(f"{group}/{name}", {"0": marr}, {
                "ome": ome, "description": about,
                "dicom": w.document(f"{group}/dicom/{name}", dicom_json(path))})
            extras.append(name)
    return {"shapes": shapes, "tile": tile, "spacing": spacing, "extras": extras,
            "codec": codec, "instances": sum(len(v) for v in kinds.values())}


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

def write_mask(w: Writer, s: dict, group: str, slide: dict) -> dict:
    kinds = {}
    for url, _ in series_objects(s["seg"]["crdc_series_uuid"]):
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

def write_nuclei(w: Writer, s: dict, group: str) -> dict:
    (url, _), = series_objects(s["ann"]["crdc_series_uuid"])
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
        "dicom": w.document(f"{group}/dicom/ann", dicom_json(path)),
        "source_url": url,
    })
    for name, ref in (("coords", g.coords), ("point_index", g.point_index), ("area", area)):
        if ref.tail_length:
            w.root[f"{nuclei}/{name}"][ref.tail_start:] = ref.tail(data)
    point_index = g.point_index.values(data)
    offset0 = np.append((point_index.astype(np.int64) - 1) // 2, g.coords.array.shape[0])
    w.native(f"{nuclei}/offset0", offset0, dims=("boundary",), chunks=(CHUNK,))
    return {"nuclei": g.count, "points": int(g.coords.array.shape[0])}


# The hierarchy
#
# A collection group states its license and citation once; a patient group
# groups that patient's slides, because a TCGA patient has several. Each
# slide is committed on its own.

def run(location: str, collection: str, slides: list[str]) -> None:
    repo = open_repository(location)
    for s in select(collection, slides):
        session = repo.writable_session("main")
        w = Writer(session)
        w.group(s["collection"], {"license": s["license"], "source_DOI": s["doi"]})
        w.group(f"{s['collection']}/{s['patient']}", {"PatientID": s["patient"]})
        group = f"{s['collection']}/{s['patient']}/{s['slide']}"
        try:
            slide = write_slide(w, s, group)
        except ValueError as e:
            say(f"{s['slide']}: refused: {e}")
            continue
        summary = {"levels": len(slide["shapes"]), "codec": slide["codec"]}
        if collection == "tcga":
            summary["mask"] = write_mask(w, s, group, slide)
            summary["nuclei"] = write_nuclei(w, s, group)
        w.group(group, {"ContainerIdentifier": s["slide"], "tissue": s["tissue"],
                        "idc": {k: s[k] for k in ("collection", "series", "crdc_series_uuid", "license", "doi")},
                        "source_prefix": f"{SOURCE}{s['crdc_series_uuid']}/"})
        say(f"{s['slide']}: {summary} -> {commit(session, f"Add slide {s['slide']}")}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--collection", choices=("gtex", "tcga"), required=True)
    p.add_argument("slides", nargs="+")
    a = p.parse_args()
    run(a.repo, a.collection, a.slides)


if __name__ == "__main__":
    main()
