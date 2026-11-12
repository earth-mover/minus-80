"""Draw RxRx19 as it is published and as the store would hold it.

    uv run --package rxrx19-ingest python ingests/rxrx19/diagrams.py

Writes ``source.svg`` and ``store.svg`` into ``diagrams/`` beside this file.

- **source.svg** is the layout on ``gs://rxrx`` today: the zips, the paths
  inside them, one PNG's place in its zip, and the metadata CSV.
- **store.svg** is the proposed store: every level of the tree with the
  attributes it carries, each built by ``layout.py``, which the ingest uses
  too. The example values are one real site, HRCE-1 plate 1 well AA02 site 2.

Every count, size and offset comes from the zips' central directories (read
once and kept as Parquet by
:func:`minus80.parsers.zip.reading.cached_central_directory`) and the metadata
CSVs. Each dataset also costs one range read of one PNG, for its header.
Before drawing, the script checks that every member's local header has the
28-byte extra field ``layout.LOCAL_EXTRA`` assumes, and stops if one does not.

Two numbers are measurements rather than reads, the constants below:
snapshot bytes per node and manifest bytes per site, from writing whole plates
of this layout to a local Icechunk 2.3.0 repo.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import layout
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv
from ingest import display_windows
from layout import DATASETS
from source import Survey, survey

from minus80 import diagrams as d
from minus80.diagrams import ACCENTS, Canvas, Line, TreeNode

OUT = Path(__file__).parent / "diagrams"

# Measured on 2026-11-11 with icechunk 2.3.0: HRCE-1 Plate2 (12,091 nodes) and HUVEC-1
# Plate1 (4,015 nodes) were written to a local repo in this layout, one commit each, and
# the growth of the snapshot and the manifests directory was divided by the plate's nodes
# and sites. Icechunk 2.3 writes one manifest per array.
SNAPSHOT_BYTES_PER_NODE = {"rxrx19a": 62.2, "rxrx19b": 66.2}
MANIFEST_BYTES_PER_SITE = {"rxrx19a": 298, "rxrx19b": 313}
#: Icechunk's default snapshot cache, in nodes.
SNAPSHOT_CACHE_NODES = 500_000

EXAMPLE = ("HRCE-1", 1, "AA02", 2)


# Shared pieces of both figures

WIDTH = 1560
TREE_X = 24
PANEL_X = 400
PANEL_WIDTH = WIDTH - PANEL_X - 24
GAP = 14


def tree_with_panels(canvas: Canvas, top: float, entries: list[tuple[TreeNode, str, list[Line], str, str]]) -> float:
    """A tree down the left, each node level with an attributes panel on the right.

    Each entry is ``(node, title, lines, kind, accent)``; the node's ``y`` is set here.
    """
    y = top
    nodes = []
    for node, title, lines, kind, accent in entries:
        bottom = d.attributes(canvas, PANEL_X, y, PANEL_WIDTH, title, lines, kind=kind, accent=accent)
        nodes.append(TreeNode(node.name, y + 18, node.depth, node.count, node.subtitle, node.status))
        y = bottom + GAP
    d.tree(canvas, TREE_X, nodes, leader_to=PANEL_X)
    return y


def gigabytes(n: int) -> str:
    return d.size_text(n)


def span_text(values) -> str:
    values = list(values)
    return d.span(min(values), max(values))


def node_count(metadata: pd.DataFrame) -> dict[str, int]:
    plates = metadata.groupby(["experiment", "plate"]).ngroups
    return {
        "experiments": metadata.experiment.nunique(),
        "plates": plates,
        "rows": plates * metadata.well.str[:-2].nunique(),
        "wells": metadata.well_id.nunique(),
        "sites": len(metadata),
    }


# The source figure


def source_summary(surveys: list[Survey]) -> str:
    pngs = sum(len(s.pngs) for s in surveys)
    total = sum(s.archive_size for s in surveys)
    return f"{pngs:,} PNGs in {gigabytes(total)} of zips, read from the zips' central directories and metadata.csv."


def source_figure(surveys: list[Survey]) -> Canvas:
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "RxRx19 as published", "gs://rxrx: per dataset, a zip of PNGs, a metadata zip and an embeddings zip")
    y = canvas.text(canvas.margin, y + 22, source_summary(surveys), size=13, color=d.MUTED)
    y = d.legend(canvas, canvas.margin, y + 14, [(ACCENTS["source"], "source"), ("removed", "the dataset's README in recursionpharma/rxrx-datasets is wrong")])
    y = tree_with_panels(canvas, y + 20, source_entries(surveys))
    metadata_table(canvas, y, surveys, WIDTH - 2 * canvas.margin)
    return canvas


def metadata_table(canvas: Canvas, y: float, surveys: list[Survey], width: float) -> float:
    a, b = surveys
    sample = a.metadata.set_index("site_id").loc[[f"HRCE-1_1_AA02_{n}" for n in (1, 2)]].reset_index()
    sample = pd.concat([sample, b.metadata.head(2)])
    columns = ["site_id", "well_id", "cell_type", "disease_condition", "treatment", "treatment_conc", "SMILES"]
    y = canvas.text(canvas.margin, y + 16, "metadata.csv, four real rows", size=14, weight="bold") + 10
    widths = [180, 150, 90, 170, 190, 120]
    widths.append(width - sum(widths))
    rows = [[d.Cell([d.fit(str(v), w - 16, 11.5, mono=True)]) for v, w in zip(r, widths)] for r in sample[columns].itertuples(index=False)]
    return d.table(canvas, canvas.margin, y, widths, columns, rows)


def source_entries(surveys: list[Survey]) -> list:
    """The source's levels, top down, as ``(node, title, lines, kind, accent)``."""
    a, b = surveys
    source = ACCENTS["source"]

    def per_dataset(fmt) -> list[Line]:
        return [Line(fmt(s)) for s in surveys]

    def zip_line(s: Survey, url: str, table: pa.Table) -> str:
        size = int(table.schema.metadata[b"archive_size"])
        return f"{url.removeprefix('gs://rxrx/'):<40} {gigabytes(size):>9}"

    def methods(s: Survey) -> str:
        counts = s.directory.group_by("method").aggregate([("name", "count")]).to_pylist()
        named = {0: "stored", 8: "Deflate"}
        deflated = s.directory.filter(pc.equal(s.directory["method"], 8))["name"].to_pylist()
        text = ", ".join(f"{named.get(c['method'], c['method'])} ×{c['name_count']:,}" for c in counts)
        return f"{s.dataset.key}: {text}   ({', '.join(n.rsplit('/', 1)[-1] for n in deflated)} deflated)"

    def experiment_lines() -> list[Line]:
        lines = []
        for s in surveys:
            for experiment, group in s.pngs.groupby("experiment"):
                wells = group.groupby(["plate", "well"]).ngroups
                lines.append(Line(
                    f"{s.dataset.folder}/images/{experiment + '/':<10} {group.plate.nunique():>3} plates "
                    f"{wells:>8,} wells {len(group):>10,} PNGs {gigabytes(int(group.bytes.sum())):>10}"
                ))
        return lines

    example_name = f"{a.dataset.folder}/images/{EXAMPLE[0]}/Plate{EXAMPLE[1]}/{EXAMPLE[2]}_s{EXAMPLE[3]}_w3.png"
    example = a.pngs.set_index("name").loc[example_name]
    letters = sorted(a.pngs.well.str[:-2].unique(), key=lambda s: (len(s), s))
    numbers = sorted(a.pngs.well.str[-2:].unique())

    entries = [
        (
            TreeNode("gs://rxrx/", 0, 0, subtitle="public bucket"),
            "gs://rxrx/",
            [Line(zip_line(s, url, table)) for s in surveys for url, table in (
                (s.dataset.images, s.directory), (s.dataset.metadata, s.metadata_zip), (s.dataset.embeddings, s.embeddings_zip))]
            + [Line(comment="CC BY 4.0; read anonymously, over gs:// or https://storage.googleapis.com/rxrx/")],
            "objects", source,
        ),
        (
            TreeNode("<dataset>-images.zip", 0, 1, count=2, subtitle="zip64"),
            "RxRx19a-images.zip, rxrx19b-images.zip",
            per_dataset(lambda s: f"{s.dataset.key}: {s.directory.num_rows:,} members; central directory {gigabytes(s.directory_size)} "
                                  f"at byte {s.directory_offset:,} of {s.archive_size:,}")
            + [Line(methods(s)) for s in surveys]
            + [Line("local header: 30 bytes + name + 28-byte extra field, then the PNG",
                    comment="checked on every member")],
            "central directory", source,
        ),
        (
            TreeNode("<experiment>/", 0, 2, count=a.pngs.experiment.nunique() + b.pngs.experiment.nunique(), subtitle="cell type, run"),
            "<folder>/images/<experiment>/",
            experiment_lines(),
            "directories in the zips", source,
        ),
        (
            TreeNode("Plate<n>/", 0, 3, count=sum(s.pngs.groupby(["experiment", "plate"]).ngroups for s in surveys), subtitle="1536-well plate"),
            "<experiment>/Plate<n>/",
            [Line(f"{s.dataset.key}: wells per plate {span_text(s.pngs.groupby(['experiment', 'plate']).well.nunique())}") for s in surveys]
            + [Line(f"well names: letters {letters[0]}–{letters[-1]} ({len(letters)}), numbers {numbers[0]}–{numbers[-1]} ({len(numbers)})",
                    comment="the inner 30 × 46 wells of a 32 × 48 plate")],
            "directories in the zips", source,
        ),
        (
            TreeNode("<well>_s<site>_w<ch>.png", 0, 4, count=len(a.pngs) + len(b.pngs), subtitle="one channel of one site"),
            example_name.split("/images/")[1],
            [
                Line("well AA02 = row AA, column 02", status="removed", tag="rxrx-datasets README is wrong"),
                Line(comment=f'rxrx-datasets/rxrx19a/README.md reads AA02 as "column AA, row 2"; but the letters ({len(letters)}) fit the '
                             f"plate's 32 rows and the numbers ({len(numbers)}) its 48 columns", status="removed"),
                Line(f"site s{EXAMPLE[3]} of {a.pngs.site.max()} (rxrx19a), of {b.pngs.site.max()} (rxrx19b)"),
                Line(f"channel w3 of w1–w{a.pngs.channel.max()} (rxrx19a), w1–w{b.pngs.channel.max()} (rxrx19b)",
                     comment="rxrx19a: w3 is Phalloidin, per its rxrx-datasets README; rxrx19b's README names no stains"),
                Line(f"local header at {int(example.offset):,}; PNG at {int(example.data_offset):,}, {int(example.bytes):,} bytes, stored"),
            ]
            + [Line(f"{s.dataset.key}: PNG {s.png_header['width']} × {s.png_header['height']}, {s.png_header['bit_depth']}-bit grayscale; "
                    f"{int(s.pngs.bytes.min()):,}–{int(s.pngs.bytes.max()):,} bytes each") for s in surveys],
            "member", source,
        ),
        (
            TreeNode("metadata.csv", 0, 1, count=2, subtitle="one row per site"),
            "RxRx19a/metadata.csv, rxrx19b/metadata.csv",
            [Line("columns: " + ", ".join(a.metadata.columns))]
            + [Line(f"{s.dataset.key}: {len(s.metadata):,} rows, one per site; "
                    f"{len(set(s.metadata.site_id) & set(site_ids(s))):,} match a site with PNGs, "
                    f"{len(set(s.metadata.site_id) ^ set(site_ids(s)))} do not") for s in surveys]
            + [Line(f"{s.dataset.key}: conditions " + ", ".join(f"{k or '(blank)'} {v:,}" for k, v in s.metadata.disease_condition.value_counts().items())) for s in surveys],
            "inside the metadata zips", source,
        ),
    ]
    return entries


def site_ids(s: Survey) -> pd.Series:
    return s.pngs.site_id


# The store figure


def store_summary(surveys: list[Survey]) -> list[str]:
    counts = {s.dataset.key: node_count(s.metadata) for s in surveys}
    nodes = {k: 2 * c["sites"] + c["wells"] + c["rows"] + c["plates"] + c["experiments"] + 1 for k, c in counts.items()}
    all_nodes = sum(nodes.values()) + 1
    snapshot = sum(SNAPSHOT_BYTES_PER_NODE[k] * n for k, n in nodes.items())
    sites = sum(c["sites"] for c in counts.values())
    manifests = sum(MANIFEST_BYTES_PER_SITE[k] * counts[k]["sites"] for k in counts)
    refs = sum(len(s.pngs) for s in surveys)
    low, high = sorted(SNAPSHOT_BYTES_PER_NODE.values())
    return [
        f"{all_nodes:,} nodes (a group and an array per site: {sites:,} sites), {refs:,} virtual references, no native chunks.",
        f"Snapshot ≈ {snapshot / 1e6:.0f} MB at the measured {low}–{high} bytes per node, above the default snapshot cache of "
        f"{SNAPSHOT_CACHE_NODES:,} nodes. Manifests: {sites:,} (one per array in Icechunk 2.3), {manifests / 1e6:.0f} MB.",
    ]


def store_figure(surveys: list[Survey]) -> Canvas:
    experiment, plate, well, site = EXAMPLE
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "RxRx19 in Icechunk: proposed layout", f"{layout.REPO.removeprefix('arraylake://')}; OME-Zarr 0.6 HCS plates; every PNG referenced in place as one chunk")
    for line in store_summary(surveys):
        y = canvas.text(canvas.margin, y + 21, line, size=13, color=d.MUTED)
    y = canvas.text(canvas.margin, y + 21, f"Example values: rxrx19a {experiment} Plate{plate}, well {well}, site {site}.", size=13, color=d.MUTED)
    y = d.legend(canvas, canvas.margin, y + 14, [(ACCENTS["store"], "store group or array"), ("changed", "judgment call or unknown"), ("added", "not in the source")])
    tree_with_panels(canvas, y + 20, store_entries(surveys))
    return canvas


def store_entries(surveys: list[Survey]) -> list:
    """The store's levels, top down, as ``(node, title, lines, kind, accent)``, filled with the example site's values."""
    a, b = surveys
    counts = {s.dataset.key: node_count(s.metadata) for s in surveys}
    nodes = {k: 2 * c["sites"] + c["wells"] + c["rows"] + c["plates"] + c["experiments"] + 1 for k, c in counts.items()}
    sites = sum(c["sites"] for c in counts.values())
    experiment, plate, well, site = EXAMPLE
    m = a.metadata
    plate_sites = m[(m.experiment == experiment) & (m.plate == plate)]
    well_sites = plate_sites[plate_sites.well == well]
    site_row = well_sites[well_sites.site == site].iloc[0]
    plate_doc = layout.plate_document(experiment, plate, list(plate_sites.well), int(plate_sites.site.max()))["ome"]["plate"]
    well_doc = layout.well_document(list(well_sites.site), well_sites.iloc[0].to_dict())
    windows = display_windows(a, [(experiment, plate)], None)[(experiment, plate)]
    site_doc = layout.site_document(a.dataset, site_row.site_id, int(a.pngs.channel.max()), windows)
    multiscale = site_doc["ome"]["multiscales"][0]
    spec = layout.array_spec(a.dataset, int(a.pngs.channel.max()))
    pngs = a.pngs[(a.pngs.experiment == experiment) & (a.pngs.plate == plate) & (a.pngs.well == well) & (a.pngs.site == site)].sort_values("channel")
    row, column = layout.split_well(well)
    store, image, array = ACCENTS["store"], "#0891b2", "#7c3aed"

    def both(key: str) -> int:
        return sum(c[key] for c in counts.values())

    rows_list = [r["name"] for r in plate_doc["rows"]]
    columns_list = [c["name"] for c in plate_doc["columns"]]
    annotations = well_doc["annotations"]
    channels = [c["label"] for c in site_doc["ome"]["omero"]["channels"]]
    entries = [
        (
            TreeNode("/", 0, 0, subtitle="root"),
            "/",
            [Line(f"{k}: {json.dumps(v)}") for k, v in layout.root_document().items()]
            + [Line(comment=f"{nodes['rxrx19a']:,} nodes under rxrx19a, {nodes['rxrx19b']:,} under rxrx19b")],
            "zarr.json attributes", store,
        ),
        (
            TreeNode("rxrx19a", 0, 1, count=2, subtitle="dataset"),
            "/rxrx19a",
            [
                Line(f'title: "{a.dataset.title}"'),
                Line(f'citation: {{text: "{a.dataset.citation}", doi: "{a.dataset.doi}"}}'),
                Line(f'source: {{images: "{a.dataset.images}", metadata: "…-metadata.zip", embeddings: "…-DL-embeddings.zip"}}'),
                Line("metadata.csv → an Iceberg table keyed by site_id", comment="mixed fields, queried by treatment or condition",
                     status="added", tag="Iceberg, not Zarr"),
                Line(f"embeddings: array (site: {len(a.metadata):,}, feature: 1024) float32, with a site_id array",
                     comment="copied from embeddings.csv; float32 is lossless for its values", status="added", tag="native copy"),
            ],
            "zarr.json attributes", store,
        ),
        (
            TreeNode(experiment, 0, 2, count=both("experiments"), subtitle="experiment"),
            f"/rxrx19a/{experiment}",
            [Line(f'cell_type: "{site_row.cell_type}"', comment="the same in every row of the experiment")]
            + [Line(comment=f"{s.dataset.key}: " + ", ".join(f"{e} ({g.plate.nunique()} plates)" for e, g in s.metadata.groupby("experiment"))) for s in surveys],
            "zarr.json attributes", store,
        ),
        (
            TreeNode(f"Plate{plate}", 0, 3, count=both("plates"), subtitle="HCS plate"),
            f"/rxrx19a/{experiment}/Plate{plate}",
            [
                Line(f'ome: {{version: "0.6", plate: {{name: "{plate_doc["name"]}", field_count: {plate_doc["field_count"]},'),
                Line(f'  rows: [{rows_list[0]} … {rows_list[-1]}],  columns: [{columns_list[0]} … {columns_list[-1]}],',
                     comment=f"all {len(rows_list)} × {len(columns_list)} of the 1536-well format", status="added", tag="from the format"),
                Line(f'  wells: [{{path: "{plate_doc["wells"][0]["path"]}"}}, … {len(plate_doc["wells"]):,} wells]}}}}',
                     comment=f"rxrx19a {span_text(a.metadata.groupby(['experiment','plate']).well.nunique())} per plate, rxrx19b {span_text(b.metadata.groupby(['experiment','plate']).well.nunique())}"),
            ],
            "zarr.json attributes", store,
        ),
        (
            TreeNode(row, 0, 4, count=both("rows"), subtitle="row"),
            f"…/Plate{plate}/{row}",
            [Line(comment="no attributes: HCS puts a row group between the plate and its wells")],
            "zarr.json attributes", store,
        ),
        (
            TreeNode(column, 0, 5, count=both("wells"), subtitle="well"),
            f"…/Plate{plate}/{row}/{column}",
            [
                Line(f'ome: {{version: "0.6", well: {{images: [' + ", ".join(f'{{path: "{i["path"]}"}}' for i in well_doc["ome"]["well"]["images"]) + "]}}"),
                Line("annotations: {", comment=f"metadata.csv, the same for every site of {well}"),
            ]
            + [Line(f"  {k}: {json.dumps(v)},") for k, v in annotations.items()]
            + [Line("}")],
            "zarr.json attributes", store,
        ),
        (
            TreeNode(str(site), 0, 6, count=sites, subtitle="site: OME-Zarr image"),
            f"…/{row}/{column}/{site}",
            [
                Line(f'ome: {{version: "0.6", multiscales: [{{axes: c (channel), y, x (space),', comment="one level: the PNGs as published"),
                Line(f'  datasets: [{{path: "0", scale: [1, {a.dataset.pixel_size}, {a.dataset.pixel_size}]}}]}}],  unit: micrometer',
                     comment=f"README: 0.65 µm per pixel; doubled for the 2048 → 1024 downsampling",
                     status="changed", tag="judgment: 0.65 × 2"),
                Line(f"  omero: {{channels: [{', '.join(channels)}]}}",
                     comment="w1–w5: the README's stains, by Cell Painting's short names",
                     status="changed", tag="judgment: Concanavalin A as ER"),
                Line("  windows: " + ", ".join(f"{s}–{e}" for s, e in windows),
                     comment=f"0.1–99.9th percentile over {layout.WINDOW_SITES} Mock sites of Plate{plate}",
                     status="added", tag="from decoded pixels"),
                Line(comment=f"rxrx19b: scale [1, {b.dataset.pixel_size}, {b.dataset.pixel_size}] µm; channels w1–w6, because its README names no stains",
                     status="changed", tag="rxrx19b stains unknown"),
                Line(f'annotations: {{site_id: "{site_row.site_id}", source_url: "{a.dataset.images}"}}'),
            ],
            "zarr.json attributes", image,
        ),
        (
            TreeNode("0", 0, 7, count=sites, subtitle="full resolution, virtual"),
            f"…/{row}/{column}/{site}/0",
            [
                Line(f"shape: {list(spec['shape'])}   chunk_shape: {list(spec['chunks'])}   data_type: {spec['dtype']}   "
                     f"dimension_names: {json.dumps(list(spec['dimension_names']))}"),
                Line(f'codecs: [{{name: "{spec["codec"]}"}}]', comment="each chunk is a whole PNG file"),
            ]
            + [Line(f"chunk c/{c}/0/0 → {a.dataset.images}  offset {int(p.data_offset):,}  length {int(p.bytes):,}",
                    comment=p.name.rsplit("/", 1)[-1]) for c, p in enumerate(pngs.itertuples())]
            + [Line(comment=f"rxrx19b: shape [{b.pngs.channel.max()}, {b.dataset.side}, {b.dataset.side}], one chunk per PNG the same way")],
            "array metadata and chunk references", array,
        ),
    ]
    return entries


# The mapping figure: the source on the left, the store on the right, each source level
# beside the store nodes it becomes, with the rule that maps them on the arrow.

MAP_WIDTH = 2540
LEFT_X, LEFT_WIDTH = 24, 1060
RIGHT_X = 1290
RIGHT_WIDTH = MAP_WIDTH - RIGHT_X - 24

#: Bands of the mapping figure: which source levels (by index in source_entries) sit beside
#: which store levels (by index in store_entries), and an arrow per rule between them, as
#: (source level, its line, store level, its line, label). Line None is the panel's title.
BANDS = [
    ([0], [0], [(0, None, 0, None, "bucket → repo root")]),
    ([1], [1], [(1, None, 1, None, "one zip → one dataset group")]),
    ([2], [2], [(2, None, 2, None, "HRCE-1/ → HRCE-1")]),
    ([3], [3], [(3, None, 3, None, "Plate1/ → Plate1, an HCS plate")]),
    (
        [4, 5],
        [4, 5, 6, 7],
        [
            (4, 0, 4, None, "AA → row AA"),
            (4, 0, 5, None, "02 → column 2"),
            (4, 2, 6, None, "s2 → site 2"),
            (4, 3, 7, 4, "w3 → chunk c/2/0/0"),
            (5, 1, 5, 1, "per-well fields → annotations"),
        ],
    ),
]


def anchor(top: float, line: int | None) -> float:
    """The y of a panel's title, or of one of its lines, for an arrow to start or end at."""
    return top + 18 if line is None else top + 34 + line * d.ATTRIBUTE_LINE + 9


def titled(entry) -> tuple[str, list[Line], str, str]:
    """An entry's panel, its title carrying the node count the tree would show."""
    node, title, lines, kind, accent = entry
    count = f"   ×{node.count:,}" if node.count else ""
    return f"{title}{count}", lines, kind, accent


def mapping_figure(surveys: list[Survey]) -> Canvas:
    experiment, plate, well, site = EXAMPLE
    source, store = source_entries(surveys), store_entries(surveys)
    canvas = Canvas(MAP_WIDTH)
    y = d.heading(canvas, "RxRx19: from the zips on gs://rxrx to the proposed store",
                  f"each source level beside the store nodes it becomes; example: rxrx19a {experiment} Plate{plate}, well {well}, site {site}")
    y = canvas.text(canvas.margin, y + 21, "Source: " + source_summary(surveys), size=13, color=d.MUTED)
    for line in store_summary(surveys):
        y = canvas.text(canvas.margin, y + 21, "Store: " + line, size=13, color=d.MUTED)
    y = d.legend(canvas, canvas.margin, y + 14, [(ACCENTS["source"], "source"), (ACCENTS["store"], "store"),
                                                 ("removed", "the rxrx-datasets README is wrong"), ("changed", "judgment call or unknown"), ("added", "not in the source"), ("arrow", "becomes")])
    y += 24
    canvas.text(LEFT_X, y, "gs://rxrx today", size=16, weight="bold")
    canvas.text(RIGHT_X, y, "proposed store", size=16, weight="bold")
    y += 18
    for left, right, arrows in BANDS:
        tops = {}
        ly = y
        for i in left:
            title, lines, kind, accent = titled(source[i])
            tops[("source", i)] = ly
            ly = d.attributes(canvas, LEFT_X, ly, LEFT_WIDTH, title, lines, kind=kind, accent=accent) + GAP
        ry = y
        for j in right:
            title, lines, kind, accent = titled(store[j])
            tops[("store", j)] = ry
            ry = d.attributes(canvas, RIGHT_X, ry, RIGHT_WIDTH, title, lines, kind=kind, accent=accent) + GAP
        for i, source_line, j, store_line, label in arrows:
            start = (LEFT_X + LEFT_WIDTH, anchor(tops[("source", i)], source_line))
            end = (RIGHT_X, anchor(tops[("store", j)], store_line))
            d.link(canvas, start, end, label=label, color=ACCENTS["highlight"] if i == 4 else d.MUTED)
        y = max(ly, ry) + 18
    metadata_table(canvas, y, surveys, MAP_WIDTH - 2 * canvas.margin)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    surveys = [survey(dataset) for dataset in DATASETS.values()]
    args.out.mkdir(parents=True, exist_ok=True)
    for name, figure in (("source", source_figure), ("mapping", mapping_figure), ("store", store_figure)):
        size = figure(surveys).write(args.out / f"{name}.svg")
        print(f"{name}.svg  {size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
