"""Draw the GTEx ingest: the bucket's slide series, how one maps onto the store, and the store.

    uv run --package idc-ingest python ingests/idc/diagrams.py STORE [--slide GTEX-111FC-0225]

Writes ``source.svg``, ``mapping.svg`` and ``store.svg`` into ``diagrams/``
beside this file. The values come from three inputs:

- idc-index's catalog gives the collection's counts: slides, donors,
  instances, bytes, levels per slide;
- one listing of the example slide's series prefix gives its objects' names,
  sizes and ETags;
- the store (a trial store or the published one), opened read-only, gives
  every shape, chunk shape, codec and attribute drawn for the example slide.

No pixel is read. LAYOUTS.md explains the mapping; the figures carry no prose.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from minus80 import diagrams as d
import zarr
from minus80.diagrams import ACCENTS, Canvas, CubeGeometry, Row, TreeNode

from ingest import catalog
from plumbing import Source, open_repository

OUT = Path(__file__).parent / "diagrams"
WIDTH = 1180


def collection_counts() -> dict:
    from idc_index import IDCClient

    client = IDCClient()
    client.fetch_index("sm_instance_index")
    rows = client.sm_instance_index.merge(client.index[["SeriesInstanceUID", "collection_id", "PatientID"]],
                                          on="SeriesInstanceUID")
    rows = rows[rows.collection_id == "gtex"]
    kinds = rows.ImageType.map(lambda t: t[2])
    levels = Counter(rows[kinds == "VOLUME"].groupby("SeriesInstanceUID").size())
    return {
        "slides": rows.SeriesInstanceUID.nunique(), "donors": rows.PatientID.nunique(), "instances": len(rows),
        "bytes": int(rows.instance_size.sum()), "volume_bytes": int(rows[kinds == "VOLUME"].instance_size.sum()),
        "kinds": Counter(kinds), "levels": levels, "version": client.get_idc_version(),
    }


def example(store, slide: str) -> dict:
    """The example slide's store nodes and its series' objects, matched by URL."""
    found = [s for s in catalog("gtex") if s["slide"] == slide]
    if not found:
        raise SystemExit(f"{slide} is not a GTEx slide")
    s = found[0]
    group = store[f"gtex/{s['patient']}/{slide}"]
    objects = {url: (size, etag) for url, size, etag in Source().objects(s["crdc_series_uuid"])}
    arrays = []
    for node in ("image", "thumbnail", "overview"):
        if node not in group:
            continue
        for name, array in sorted(group[node].arrays()):
            url = array.attrs["source_url"]
            arrays.append({"node": f"{node}/{name}", "array": array, "url": url, "size": objects[url][0],
                           "etag": objects[url][1]})
    return {"slide": s, "group": group, "arrays": arrays, "objects": objects}


def codec_text(array) -> str:
    names = [c["name"].removeprefix("imagecodecs_") for c in array.metadata.to_dict()["codecs"]]
    return " + ".join(names)


def source_figure(counts: dict, ex: dict) -> Canvas:
    c = Canvas(WIDTH)
    y = d.heading(c, "Source: GTEx slides in IDC's bucket",
                  f"s3://idc-open-data/<crdc_series_uuid>/<crdc_instance_uuid>.dcm · IDC {counts['version']}") + 30
    levels = ", ".join(f"{n} on {k:,}" for n, k in sorted(counts["levels"].items()))
    d.panel(c, 24, y, 520, "collection gtex", [
        Row("slides", f"{counts['slides']:,} series"),
        Row("donors", f"{counts['donors']:,}"),
        Row("instances", f"{counts['instances']:,}"),
        Row("bytes", d.size_text(counts["bytes"])),
        Row("VOLUME", f"{counts['kinds']['VOLUME']:,} · {d.size_text(counts['volume_bytes'])} JPEG"),
        Row("THUMBNAIL", f"{counts['kinds']['THUMBNAIL']:,} · uncompressed"),
        Row("OVERVIEW", f"{counts['kinds']['OVERVIEW']:,} · uncompressed"),
        Row("levels", levels),
    ], accent=ACCENTS["source"], source="idc-index sm_instance_index")
    s = ex["slide"]
    tree_y = y + 14
    nodes = [TreeNode("s3://idc-open-data/", tree_y),
             TreeNode(f"{s['crdc_series_uuid'][:8]}…/", tree_y + 40, 1, counts["slides"], s["slide"])]
    rows = []
    for i, a in enumerate(ex["arrays"]):
        name = a["url"].rsplit("/", 1)[1]
        nodes.append(TreeNode(f"{name[:8]}….dcm", tree_y + 90 + i * 34, 2))
        array = a["array"]
        kind = "VOLUME" if a["node"].startswith("image/") else a["node"].split("/")[0].upper()
        rows.append(Row(f"{name[:8]}…", f"{kind} {d.dims(array.shape[1:])} · {d.size_text(a['size'])}"))
    d.tree(c, 580, nodes)
    d.panel(c, 820, tree_y - 14, 336, f"series of {s['slide']}", rows, accent=ACCENTS["source"],
            source="bucket listing")
    return c


def mapping_figure(ex: dict) -> Canvas:
    c = Canvas(WIDTH)
    s = ex["slide"]
    y = d.heading(c, f"Mapping: slide {s['slide']}", "each DICOM instance becomes one array; each frame one chunk") + 36
    row_gap = 180
    for i, a in enumerate(ex["arrays"]):
        array = a["array"]
        top = y + i * row_gap
        kind = "VOLUME" if a["node"].startswith("image") else a["node"].split("/")[0].upper()
        name = a["url"].rsplit("/", 1)[1]
        frames = array.nchunks
        d.panel(c, 24, top, 380, f"{name[:13]}….dcm", [
            Row("ImageType", kind),
            Row("matrix", d.dims(array.shape[1:])),
            Row("frames", f"{frames:,} of {d.dims(array.chunks[1:])}"),
            Row("ETag", a["etag"][:22]),
        ], accent=ACCENTS["source"], source=d.size_text(a["size"]))
        h, w = array.shape[1:]
        scale = 80 / max(h, w)
        geometry = CubeGeometry(700, top, max(w * scale, 12), max(h * scale, 12), 14)
        grid = (d.grid_lines(w, array.chunks[2]), d.grid_lines(h, array.chunks[1])) if frames <= 400 else None
        d.cube(c, geometry, a["node"], [f"{d.dims(array.shape)} · chunks {d.dims(array.chunks)}", codec_text(array)],
               grid=grid, label_width=460)
        d.link(c, (404, top + 50), (geometry.x - 6, geometry.face_top + geometry.height / 2),
               label="frames → chunks", color=ACCENTS["source"])
    bottom = y + len(ex["arrays"]) * row_gap
    d.panel(c, 24, bottom, 380, "every instance's header", [Row("pixel data", "excluded")],
            accent=ACCENTS["source"], source="DICOM JSON")
    d.panel(c, 700, bottom, 460, "dicom", [Row("dtype", "uint8 · one zstd chunk"), Row("keys", "image/0 … overview")])
    d.link(c, (404, bottom + 40), (700, bottom + 40), label="one document", color=ACCENTS["source"])
    d.legend(c, 24, bottom + 110, [(ACCENTS["source"], "source object"), (ACCENTS["store"], "store node"),
                                  ("arrow", "referenced in place, pinned to the ETag")])
    return c


def store_figure(store, counts: dict, ex: dict) -> Canvas:
    c = Canvas(WIDTH)
    root = store
    y = d.heading(c, "Store: OME-Zarr 0.6 in Icechunk", root.attrs.get("title", "")) + 40
    image = ex["group"]["image"]
    ms = image.attrs["ome"]["multiscales"][0]
    scale0 = ms["datasets"][0]["coordinateTransformations"][0]["transformations"][0]["scale"]
    levels = len(ms["datasets"])
    spec = ex["slide"]["specimen"]
    step = 100
    nodes = [
        TreeNode("/", y, 0, None, "title · idc_version · codecs · left_out"),
        TreeNode("gtex", y + step, 1, None, "license · source_DOI · citations"),
        TreeNode("<donor>", y + 2 * step, 2, counts["donors"], "PatientID"),
        TreeNode("<slide>", y + 3 * step, 3, counts["slides"], "specimen · idc · source_prefix"),
        TreeNode("image", y + 4 * step, 4, None, f"multiscale, {levels} levels here"),
        TreeNode("thumbnail", y + 5 * step, 4, None, "one level"),
        TreeNode("overview", y + 6 * step, 4, None, "one level, no unit"),
        TreeNode("dicom", y + 7 * step, 4, None, "uint8 JSON"),
    ]
    d.tree(c, 24, nodes, leader_to=520)
    panels = [
        ("/", [Row("idc_version", str(root.attrs.get("idc_version", ""))),
               Row("codecs", ", ".join(n.removeprefix("imagecodecs_") for n in root.attrs.get("codecs", {}))),
               Row("left_out", f"{len(root.attrs.get('left_out', {}))} slides")]),
        ("gtex", [Row("license", ex["slide"]["idc"]["license"]), Row("source_DOI", ex["slide"]["idc"]["source_DOI"])]),
        (None, []),
        (ex["slide"]["slide"], [Row("tissue", spec["tissue"]), Row("stain", " + ".join(spec["stain"])),
                                Row("fixative", ", ".join(spec["fixative"]))]),
        ("image", [Row("axes", "c, y, x (µm)"), Row("level 0", f"{d.dims(image['0'].shape)} @ {scale0[1]} µm"),
                   Row("chunk", f"{d.dims(image['0'].chunks)} · {codec_text(image['0'])}")]),
    ]
    for i, (title, rows) in enumerate(panels):
        if title:
            d.panel(c, 520, y + i * step - 14, 636, title, rows, accent=ACCENTS["store"])
    return c


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("store", help="a local repo path or arraylake://org/repo")
    p.add_argument("--slide", default="GTEX-111FC-0225")
    a = p.parse_args()
    store = zarr.open_group(open_repository(a.store).readonly_session("main").store, mode="r")
    counts = collection_counts()
    ex = example(store, a.slide)
    OUT.mkdir(exist_ok=True)
    for name, canvas in (("source", source_figure(counts, ex)), ("mapping", mapping_figure(ex)),
                         ("store", store_figure(store, counts, ex))):
        print(f"{name}.svg: {canvas.write(OUT / f'{name}.svg'):,} bytes")


if __name__ == "__main__":
    main()
