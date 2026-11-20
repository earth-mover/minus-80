"""Draw the proposed phase 2 of the MICrONS ingest: the processed NWB data and the CAVE match tables.

    uv run --package microns-ingest --with pandas python ingests/microns/diagrams_processed.py \\
        --catalog <catalog.json> --rois <roi_counts.json> --cave <directory of CAVE dumps>

Writes ``processed-source.svg``, ``processed-mapping.svg`` and ``processed-store.svg`` into
``diagrams/`` beside this file. They are proposals: nothing in them has been written to the
published store. The values come from three inputs, all read from the sources:

- ``catalog.json``: every object in the NWB file of scan 4-7 with its shape, dtype, chunks and
  compression, from a remote walk with h5py.
- ``roi_counts.json``: each scan's ROI count per field, and how many of those ROIs carry an EM
  point in the NWB file.
- a directory of CAVE's static table dumps, ``<version>/<table>.csv.gz`` with
  ``<table>_header.csv`` beside it, copied from ``gs://mat_dbs/public/minnie65_phase3_v1/``.

The figures live in their own script, beside phase 1's ``diagrams.py``, because phase 2 has its
own plan (PLAN-phase2.md) and phase 1's figures draw the store as it is published.
LAYOUTS.md explains the mapping; the figures carry no prose.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from minus80 import diagrams as d
from minus80.diagrams import ACCENTS, Canvas, Cell, CubeGeometry, Row, TreeNode

OUT = Path(__file__).parent / "diagrams"
WIDTH = 1400
LEFT = 24
EXAMPLE_SCAN = "4-7"
EXAMPLE_FIELD = 4
MATCH_TABLES = ("functional_coreg", "func_unit_em_match_release", "apl_functional_coreg_forward_v5",
                "coregistration_manual_v3", "coregistration_manual_v4", "apl_functional_coreg_vess_fwd",
                "coregistration_auto_phase3_fwd", "coregistration_auto_phase3_fwd_apl_vess_combined",
                "coregistration_auto_phase3_fwd_v2", "coregistration_auto_phase3_fwd_apl_vess_combined_v2")


def read_cave(directory: Path) -> pd.DataFrame:
    """One row per (version, table): rows, units, and how many root IDs changed since the table's previous version."""
    out, previous = [], {}
    versions = sorted((p.name for p in directory.iterdir() if p.is_dir()), key=lambda v: int(v[1:]))
    for version in versions:
        for path in sorted((directory / version).glob("*.csv.gz")):
            table = path.name.removesuffix(".csv.gz")
            header = pd.read_csv(directory / version / f"{table}_header.csv", header=None)
            frame = pd.read_csv(path, header=None, names=list(header[0]), usecols=["id", "pt_root_id"])
            changed = None
            if table in previous:
                joined = previous[table].merge(frame, on="id", suffixes=("_a", "_b"))
                changed = int((joined.pt_root_id_a != joined.pt_root_id_b).sum())
            out.append({"version": version, "table": table, "rows": len(frame), "changed": changed})
            previous[table] = frame
    return pd.DataFrame(out)


def dataset(catalog: dict, path: str) -> dict:
    found = [o for o in catalog["objects"] if o["path"] == path]
    if not found:
        raise KeyError(f"{path} is not in the catalog")
    return found[0]


def shape_text(shape) -> str:
    return "(" + ", ".join(f"{n:,}" for n in shape) + ")"


def chunk_text(o: dict) -> str:
    return f"chunks {shape_text(o['chunks'])} {o['compression']}" if o["chunks"] else "contiguous"


def source_figure(catalog: dict, rois: dict, cave: pd.DataFrame) -> Canvas:
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Phase 2 source: processed data and EM matches",
                  f"DANDI 000402, {catalog['path'].rsplit('/', 1)[-1]}; CAVE static dumps") + 24
    k = EXAMPLE_FIELD
    groups = [
        ("ImageSegmentation/PlaneSegmentation<n>", f"processing/ophys/ImageSegmentation/PlaneSegmentation{k}",
         ["image_mask", "id", "mask_type", "pt_x_position", "pt_root_id"]),
        ("Fluorescence/RoiResponseSeries<n>", f"processing/ophys/Fluorescence/RoiResponseSeries{k}",
         ["data", "timestamps"]),
        ("SegmentationImages<n>", f"processing/ophys/SegmentationImages{k}", ["average", "correlation"]),
        ("acquisition: behavior", "acquisition", ["EyeTracking/eye_position/data",
                                                  "PupilTracking/pupil_major_radius/data",
                                                  "treadmill_velocity/data"]),
        ("acquisition: stimulus video", "acquisition/Video: stimulus_17797_4_7_v4", ["data", "timestamps"]),
    ]
    x, column_width = LEFT, 620
    top = y
    for title, base, names in groups:
        rows = []
        for name in names:
            o = dataset(catalog, f"{base}/{name}")
            rows.append(Row(name.rsplit("/", 2)[-2] if "/" in name else name,
                            f"{shape_text(o['shape'])} {o['dtype']}, {chunk_text(o)}"))
        y = d.panel(canvas, x, y, column_width, title, rows, accent=ACCENTS["source"],
                    source=f"field {k} of 8 shown" if "<n>" in title else None) + 14
    counts = rois[EXAMPLE_SCAN]
    y = d.panel(canvas, x, y, column_width, "all 19 scans", [
        Row("ROIs", f"{sum(sum(c[0] for c in v) for v in rois.values()):,} in {sum(len(v) for v in rois.values())} fields"),
        Row("with NWB EM point", f"{sum(sum(c[1] for c in v) for v in rois.values()):,}"),
        Row(f"scan {EXAMPLE_SCAN}", f"{sum(c[0] for c in counts):,} ROIs; fields {', '.join(str(c[0]) for c in counts)}"),
        Row("objects in the file", f"{len(catalog['objects'])}; full walk {catalog['seconds']:.0f} s, "
                                   f"{catalog['requests']:,} requests"),
    ], accent=ACCENTS["source"])

    x2 = LEFT + column_width + 20
    versions = list(dict.fromkeys(cave.version))
    tables = [t for t in MATCH_TABLES if t in set(cave.table)]
    widths = [270] + [52] * len(versions)
    cells = []
    for table in tables:
        row = [Cell([table.replace("coregistration_", "coreg_").replace("_phase3_fwd", "_p3")
                      .replace("apl_functional_coreg", "apl_coreg").replace("_combined", "")])]
        for version in versions:
            hit = cave[(cave.version == version) & (cave.table == table)]
            if hit.empty:
                row.append(Cell([]))
                continue
            r = hit.iloc[0]
            if pd.isna(r.changed):
                row.append(Cell([f"{r.rows / 1000:.1f}k"], status="added"))
            else:
                row.append(Cell([f"{100 * r.changed / r.rows:.1f}%"], status="changed"))
        cells.append(row)
    canvas.text(x2, top + 14, "CAVE match tables: rows when first seen, then % of root IDs changed",
                size=13, weight="bold")
    bottom = d.table(canvas, x2, top + 26, widths, ["table"] + versions, cells, row_height=24)
    d.legend(canvas, x2, bottom + 14, [("added", "first version: rows"), ("changed", "root IDs changed")])
    return canvas


def mapping_figure(catalog: dict, rois: dict) -> Canvas:
    canvas = Canvas(WIDTH)
    k = EXAMPLE_FIELD
    n = rois[EXAMPLE_SCAN][k - 1][0]
    units = sum(c[0] for c in rois[EXAMPLE_SCAN])
    y = d.heading(canvas, f"Phase 2 mapping, scan {EXAMPLE_SCAN} field {k}",
                  "NWB dataset or CAVE table → store array, under two-photon/" + EXAMPLE_SCAN) + 30
    masks = dataset(catalog, f"processing/ophys/ImageSegmentation/PlaneSegmentation{k}/image_mask")
    traces = dataset(catalog, f"processing/ophys/Fluorescence/RoiResponseSeries{k}/data")
    video = dataset(catalog, "acquisition/Video: stimulus_17797_4_7_v4/data")
    movie = dataset(catalog, f"acquisition/TwoPhotonSeries{k}/data")
    nwb = "nwb/ (the whole file)"
    pairs = [
        ("image_mask", [shape_text(masks["shape"]), f"chunks {shape_text(masks['chunks'])}", "gzip, float32"],
         f"rois-{k}/0", [shape_text((n, 1, 440, 248)), "one chunk per ROI, one shard", "blosc zstd, native"],
         "transposed and written natively", ACCENTS["highlight"]),
        ("image_mask", ["the same dataset"],
         f"nwb/…/PlaneSegmentation{k}/image_mask", ["as stored", "referenced"],
         nwb, ACCENTS["store"]),
        ("RoiResponseSeries data", [shape_text(traces["shape"]), f"chunks {shape_text(traces['chunks'])}", "gzip, float32"],
         f"nwb/…/RoiResponseSeries{k}/data", [f"dims (time, roi)", "as stored", "referenced"],
         nwb, ACCENTS["store"]),
        ("TwoPhotonSeries data", [shape_text(movie["shape"]), f"chunks {shape_text(movie['chunks'])}", "gzip, int16"],
         f"nwb/acquisition/TwoPhotonSeries{k}/data", ["as stored, referenced", f"same frames as field-{k}/0,", "transposed"],
         nwb, ACCENTS["store"]),
        ("Video data", [shape_text(video["shape"]), "one frame per chunk", "gzip, uint8"],
         "nwb/acquisition/Video: …/data", ["as stored", "referenced"],
         nwb, ACCENTS["store"]),
        ("CAVE rows (session, scan, unit_id)", ["one row per match", "csv.gz, whole-file gzip"],
         "units/em/<table>/pt_root_id", [f"({units:,},) uint64", "one chunk", "blosc zstd, native"],
         "unit_id − 1 → index; one commit per version", ACCENTS["highlight"]),
    ]
    for left, left_lines, right, right_lines, label, accent in pairs:
        a = CubeGeometry(LEFT + 10, y, 90, 50, 22)
        bottom_a = d.cube(canvas, a, left, left_lines, accent=ACCENTS["source"], label_width=360)
        b = CubeGeometry(760, y, 90, 50, 22)
        bottom_b = d.cube(canvas, b, right, right_lines, accent=accent, label_width=560)
        d.link(canvas, (a.right + 8, a.face_top + 25), (b.x - 8, b.face_top + 25), label=label)
        y = max(bottom_a, bottom_b) + 22
    edges = [sum(c[0] for c in rois[EXAMPLE_SCAN][:i]) for i in range(len(rois[EXAMPLE_SCAN]))]
    canvas.text(LEFT, y + 8, f"unit_id = (ROIs in fields 1 … n−1) + ROI id;  field {k} starts after "
                f"{edges[k - 1]:,}, so ROI 23 is unit {edges[k - 1] + 23:,}", size=12.5, mono=True)
    d.legend(canvas, LEFT, y + 28, [(ACCENTS["source"], "source"), (ACCENTS["store"], "referenced"),
                                    (ACCENTS["highlight"], "written natively")])
    return canvas


def store_figure(catalog: dict, rois: dict, cave: pd.DataFrame) -> Canvas:
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Phase 2 store, one scan", "proposed nodes beside phase 1's; tags after the run") + 30
    k = EXAMPLE_FIELD
    n = rois[EXAMPLE_SCAN][k - 1][0]
    units = sum(c[0] for c in rois[EXAMPLE_SCAN])
    spacing = 46
    entries = [
        (TreeNode(f"two-photon/{EXAMPLE_SCAN}", 0, 0, subtitle="scene; scan attribute gains 'processed'"),
         "scene", [Row("ome.scene", "field-n placed in motor (t, z, y, x)")]),
        (TreeNode("field-n", 0, 1, count=8, subtitle="phase 1"), "image",
         [Row("0", "(40,000, 1, 440, 248) int16, TIFF pages")]),
        (TreeNode("volume_times", 0, 1, subtitle="phase 1"), "array", [Row("", "(40,000,) float64")]),
        (TreeNode("rois-n", 0, 1, count=8, status="added"), "image, written natively", [
            Row("0", f"(roi, z, y, x) float32; {n:,} ROIs in field {k}"),
            Row("chunks", "one per ROI, one shard per field, blosc zstd"),
            Row("placed", "its own translation = field-n's place")]),
        (TreeNode("units", 0, 1, status="added"), "group, written natively", [
            Row("unit_id, field, roi", f"({units:,},) int64"),
            Row("em/<table>/", "pt_root_id, pt_supervoxel_id, id, target_id, pt_position, residual, score")]),
        (TreeNode("nwb", 0, 1, status="added"), "NWBFile, unified NWB Zarr v3, referenced", [
            Row("processing/ophys/", "PlaneSegmentation, Fluorescence, SegmentationImages × 8"),
            Row("acquisition/", "TwoPhotonSeries × 8, eye, pupil, treadmill, stimulus Video"),
            Row("intervals/", "Clip, Monet2, Trippy"),
            Row("general/, specifications/", "imaging planes, device, NWB schema")]),
    ]
    versions = list(dict.fromkeys(cave.version))
    entries.append((TreeNode("tags", 0, 0, count=len(versions) + 3, status="added"), "icechunk", [
        Row("v1, v2", "phase 1"), Row("processed", "nwb, rois-n, units"),
        Row(", ".join(f"cave-{v}" for v in versions[:4]), "one commit per CAVE version"),
        Row(", ".join(f"cave-{v}" for v in versions[4:]), "")]))
    nodes, top = [], y
    for node, _, rows in entries:
        nodes.append(TreeNode(node.name, top, node.depth, node.count, node.subtitle, node.status))
        top += max(spacing, d.panel_height(len(rows), source=True) + 16)
    d.tree(canvas, LEFT, nodes, leader_to=380)
    for node, (_, kind, rows) in zip(nodes, entries):
        d.panel(canvas, 380, node.y - 14, 960, node.name, rows,
                accent=ACCENTS["highlight"] if node.status else ACCENTS["store"], source=kind)
    d.legend(canvas, LEFT, top + 10, [(ACCENTS["store"], "phase 1"), (ACCENTS["highlight"], "proposed")])
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--rois", type=Path, required=True)
    parser.add_argument("--cave", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text())
    rois = json.loads(args.rois.read_text())
    cave = read_cave(args.cave)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, figure in (("processed-source", lambda: source_figure(catalog, rois, cave)),
                         ("processed-mapping", lambda: mapping_figure(catalog, rois)),
                         ("processed-store", lambda: store_figure(catalog, rois, cave))):
        size = figure().write(args.out / f"{name}.svg")
        print(f"{name}.svg  {size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
