"""Draw the MICrONS ingest: what the archive publishes, how it maps onto the store, and the store.

    uv run --package microns-ingest python ingests/microns/diagrams.py --survey <survey.json>

Writes ``source.svg``, ``mapping.svg`` and ``store.svg`` into ``diagrams/`` beside this file.
The store does not exist yet, so the mapping and store figures draw the layout PLAN.md
proposes, with numbers computed from the survey.

``survey.json`` is written by ``survey.py`` and supplies:

- ``directories``: object count, bytes and file extensions per BossDB directory, and the
  full file list of small directories. A directory the survey did not list is drawn as
  "not surveyed", never as empty.
- ``precomputed_info``: each Neuroglancer layer's ``info``. It gives the data type, the
  scales, and each scale's size, chunk shape, resolution, voxel offset, encoding and
  sharding. The store's array shapes and chunks are these values with the axes reversed,
  from the precomputed order (x, y, z) to the store's (z, y, x).
- ``dandi_000402``: the 19 NWB files' paths and sizes.

The facts below come from neither, so they are constants with their sources. The
BossDB layouts of the two-photon scans are in a PDF, and the NWB internals were read
from one file.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from minus80 import diagrams as d
from minus80.diagrams import ACCENTS, Canvas, CubeGeometry, Row, TreeNode

OUT = Path(__file__).parent / "diagrams"
WIDTH = 1400

# Two-photon scans, from BossDB's
# functional_data/two_photon_functional_scans/two_photon_functional_scans_technical_documentation_v2.pdf.
# Every scan is one TIFF in each of versions v1 and v2. A TIFF holds volumes × fields frames of
# 440 × 248 int16 pixels (600 × 372 for scan 9-6), field-major: page = volume × fields + field.
SCANS_PDF = "functional_data/two_photon_functional_scans/two_photon_functional_scans_technical_documentation_v2.pdf"


@dataclass(frozen=True)
class ScanGroup:
    """Scans that share a frame geometry and rate."""

    label: str
    scans: int
    volumes: int
    fields: int
    rate_hz: float
    frame: tuple[int, int]
    #: Pixel size in micrometers. 2.5 is each NWB's ImagingPlane grid_spacing; 1.67 for scan 9-6
    #: is PLAN.md's, not yet read from that scan's NWB.
    pixel_um: float


SCAN_GROUPS = (
    ScanGroup("15 scans", 15, 40_000, 8, 6.3, (440, 248), 2.5),
    ScanGroup("scan 17797_4_9", 1, 35_112, 8, 6.3, (440, 248), 2.5),
    ScanGroup("scans 9-3, 9-4", 2, 50_000, 6, 8.6, (440, 248), 2.5),
    ScanGroup("scan 9-6", 1, 57_000, 4, 9.6, (600, 372), 1.67),
)
EXAMPLE_SCAN = "4-7"
EXAMPLE_TIFF = "functional_scan_17797_4_7_v2.tif"

# Probed on 2026-11-12 in sub-17797_ses-4-scan-7_behavior+image+ophys.nwb (DANDI 000402). The 40,000 ×
# 248 × 440 series are one per field; the stimulus ImageSeries has one frame per chunk.
NWB_SERIES = "(40000, 248, 440) int16 gzip (2254, 14, 14)"
NWB_STIMULUS = "(381054, 144, 256, 3) uint8"

# The proofread segmentation's later releases are on Google Cloud only (MICrONS tutorial,
# "static repositories"). v117 is BossDB's minnie65/seg, from PLAN.md.
GOOGLE_RELEASES = ("v343", "v943", "v1300")
GOOGLE_PREFIX = "gs://iarpa_microns/minnie/minnie65/"

# PLAN.md decision 6: the store's codec for each precomputed encoding. Only `raw` was read
# (gzip inside the shard, minnie65/em level 0); the other two need codecs from `minus80`.
STORE_CODECS = {
    "raw": "gzip",
    "jpeg": "jpeg (minus80)",
    "compressed_segmentation": "compressed_segmentation (minus80)",
}

# The tables PLAN.md lists beside the store.
TABLES = (
    "synapses_minnie65",
    "synapses_minnie35",
    "nucleus_detection",
    "nucleus_neuron_classification",
    "cell_types",
    "proofreading_status",
    "functional_coregistration",
    "digital_twin_units",
    "files",
)

# The BossDB directory whose contents the scan constants describe; it is shown as "not surveyed"
# when the survey did not list it.
FUNCTIONAL_DIRECTORY = "functional_data"

EM_EXAMPLE = "minnie65/em"
SEGMENTATION_EXAMPLE = "minnie65/seg"
SCAN_FIELDS = max(g.fields for g in SCAN_GROUPS)


# Reading the survey


@dataclass
class Layer:
    """One Neuroglancer precomputed layer: its ``info`` and what the directory listing says about it."""

    path: str
    info: dict
    #: Objects and bytes per scale, for the scales whose directory the survey listed.
    listed: dict[str, tuple[int, int]]

    @property
    def top(self) -> str:
        return self.path.split("/")[0]

    @property
    def name(self) -> str:
        return self.path.split("/", 1)[1]

    @property
    def scales(self) -> list[dict]:
        return self.info["scales"]

    @property
    def dtype(self) -> str:
        return self.info["data_type"]

    def shape(self, scale: dict) -> tuple[int, ...]:
        return tuple(scale["size"][::-1])

    def chunks(self, scale: dict) -> tuple[int, ...]:
        return tuple(scale["chunk_sizes"][0][::-1])

    def resolution(self, scale: dict) -> tuple[int, ...]:
        return tuple(scale["resolution"][::-1])

    def translation(self, scale: dict) -> tuple[int, ...]:
        return tuple(o * r for o, r in zip(scale["voxel_offset"][::-1], scale["resolution"][::-1], strict=True))

    def encodings(self) -> list[str]:
        return list(dict.fromkeys(s["encoding"] for s in self.scales))

    def sharding(self) -> str:
        sharded = sum(1 for s in self.scales if s.get("sharding"))
        if sharded == len(self.scales):
            return "sharded"
        return "unsharded" if sharded == 0 else f"{sharded} of {len(self.scales)} scales sharded"

    def listing(self) -> str:
        """What the survey found in this layer's scale directories, or that it found none."""
        if not self.listed:
            return "not surveyed"
        objects = sum(o for o, _ in self.listed.values())
        size = sum(b for _, b in self.listed.values())
        scope = "" if len(self.listed) == len(self.scales) else f"{len(self.listed)} of {len(self.scales)} scales: "
        return f"{scope}{objects:,} objects, {d.size_text(size)}"


@dataclass
class Survey:
    """The survey's three sections, with the layers worked out from them."""

    root: str
    bucket: str
    directories: dict[str, dict]
    layers: list[Layer]
    nwb: list[dict]

    def layer(self, path: str) -> Layer:
        return next(layer for layer in self.layers if layer.path == path)

    def tops(self) -> list[str]:
        """Top-level directories, layers first, then the functional data, then whatever else was listed."""
        first = list(dict.fromkeys(layer.top for layer in self.layers))
        # The root's own entry has an empty name and holds only S3 folder markers.
        seen = {p.split("/")[0] for p in self.directories} - {""}
        rest = [t for t in sorted(seen | {FUNCTIONAL_DIRECTORY}) if t not in first]
        rest.sort(key=lambda t: t != FUNCTIONAL_DIRECTORY)
        return first + rest

    def groups(self, top: str) -> dict[str, list[str]]:
        """The directories under ``top``, grouped by their first subdirectory, in listing order."""
        groups: dict[str, list[str]] = defaultdict(list)
        for path in self.directories:
            parts = path.rstrip("/").split("/")
            if parts[0] == top:
                groups["/".join(parts[:2]) + "/" if len(parts) > 1 else top + "/"].append(path)
        return groups

    def csv_files(self) -> list[tuple[str, str, int]]:
        """Each listed CSV as ``(directory, file name, bytes)``."""
        return [
            (path.rstrip("/"), name, size)
            for path, entry in self.directories.items()
            for name, size in entry.get("files", {}).items()
            if name.endswith(".csv")
        ]


def read_survey(path: Path) -> Survey:
    raw = json.loads(path.read_text())
    directories = raw["directories"]
    layers = []
    for layer_path, info in raw["precomputed_info"].items():
        listed = {}
        for scale in info["scales"]:
            entry = directories.get(f"{layer_path}/{scale['key']}/")
            if entry:
                listed[scale["key"]] = (entry["objects"], entry["bytes"])
        layers.append(Layer(layer_path, info, listed))
    return Survey(raw["root"], raw["bucket"], directories, layers, raw["dandi_000402"])


def dandi_scan(asset: dict) -> str:
    """``4-7`` for the file of session 4, scan 7."""
    session, scan = re.search(r"ses-(\d+)-scan-(\d+)", asset["path"]).groups()
    return f"{session}-{scan}"


def dims(shape) -> str:
    """A shape as ``13,088 × 180,224 × 212,992``."""
    return " × ".join(f"{n:,}" for n in shape)


def extension_text(extensions: Counter) -> str:
    return ", ".join(f"{e or '(none)'} ×{n:,}" for e, n in extensions.most_common(3))


# Layout shared by the tree figures: a tree down the left, a panel for each node on the right.

TREE_X = 24
PANEL_X = 380
PANEL_WIDTH = 980
GAP = 16


def tree_figure(canvas: Canvas, top: float, entries: list[tuple[TreeNode, dict]]) -> float:
    """A tree with one panel per node, each node level with its panel's title.

    Each entry is a node (its ``y`` is ignored) and the keyword arguments of its
    :func:`~minus80.diagrams.panel`.
    """
    y = top
    nodes = []
    for node, spec in entries:
        bottom = d.panel(canvas, PANEL_X, y, PANEL_WIDTH, **spec)
        nodes.append(TreeNode(node.name, y + 15, node.depth, node.count, node.subtitle, node.status))
        y = bottom + GAP
    d.tree(canvas, TREE_X, nodes, leader_to=PANEL_X)
    return y


def layer_rows(layers: list[Layer], *, detail: bool) -> list[Row]:
    """Two lines per layer for the source figure (kind, then listing), one per layer for the store's."""
    rows = []
    for layer in layers:
        if detail:
            kind = f"{layer.info['type']} {layer.dtype}, {len(layer.scales)} scales, {', '.join(layer.encodings())}, {layer.sharding()}"
            rows += [Row(layer.name, kind), Row("listed", layer.listing())]
        else:
            first = layer.scales[0]
            rows.append(
                Row(
                    layer.path,
                    f"{layer.dtype}  {len(layer.scales)} levels  level 0 {dims(layer.shape(first))}  "
                    f"chunks {dims(layer.chunks(first))}",
                )
            )
    return rows


# The source figure


def source_figure(survey: Survey) -> Canvas:
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Source", f"{survey.bucket}/{survey.root}, DANDI 000402, gs://iarpa_microns")
    y = d.legend(
        canvas, canvas.margin, y + 14, [(ACCENTS["source"], "source"), ("added", "ingest"), ("changed", "not surveyed")]
    )

    listed_objects = sum(e["objects"] for e in survey.directories.values())
    listed_bytes = sum(e["bytes"] for e in survey.directories.values())
    tops = survey.tops()
    entries: list[tuple[TreeNode, dict]] = [
        (
            TreeNode(survey.root.rstrip("/").rsplit("/", 1)[-1] + "/", 0, 0, subtitle="BossDB"),
            dict(
                title=f"s3://{survey.bucket}/{survey.root}",
                source="BossDB Open Data, us-east-1",
                accent=ACCENTS["source"],
                rows=[
                    Row("top level", ", ".join(t for t in tops if any(p.split("/")[0] == t for p in survey.directories))),
                    Row("listed", f"{len(survey.directories):,} directories, {listed_objects:,} objects, {d.size_text(listed_bytes)}"),
                ],
            ),
        )
    ]

    for top in tops:
        layers = [layer for layer in survey.layers if layer.top == top]
        groups = survey.groups(top)
        if layers:
            rows = layer_rows(layers, detail=True)
            count, subtitle = len(layers), "Neuroglancer precomputed layers"
        elif groups:
            rows = []
            for group, paths in groups.items():
                entries_here = [survey.directories[p] for p in paths]
                extensions = sum((Counter(e["extensions"]) for e in entries_here), Counter())
                size = d.size_text(sum(e["bytes"] for e in entries_here))
                many = f"{len(paths)} directories, " if len(paths) > 1 else ""
                rows.append(Row(group, f"{many}{sum(e['objects'] for e in entries_here):,} objects, {size}, {extension_text(extensions)}"))
            count, subtitle = len(groups), None
        else:
            rows = [Row("listing", "none", status="changed", tag="not surveyed")]
            count, subtitle = None, None
        entries.append(
            (
                TreeNode(f"{top}/", 0, 1, count, subtitle),
                dict(title=f"{top}/", source="BossDB", accent=ACCENTS["source"], rows=rows),
            )
        )
        if top == FUNCTIONAL_DIRECTORY:
            entries.append((TreeNode("two_photon_functional_scans/", 0, 2, subtitle="layout from the PDF", status="added"), dict(
                title="two-photon scans",
                source=SCANS_PDF.rsplit("/", 1)[-1],
                accent=ACCENTS["source"],
                rows=[
                    Row("v2 TIFFs", f"{sum(g.scans for g in SCAN_GROUPS)}, int16, 2023", status="added", tag="ingest"),
                    Row("v1 TIFFs", f"{sum(g.scans for g in SCAN_GROUPS)}, uint16, 2021"),
                    Row("frame", "440 × 248 int16, field-major (600 × 372 for scan 9-6)"),
                    *[
                        Row(g.label, f"{g.volumes:,} volumes × {g.fields} fields, {g.rate_hz} Hz, {g.volumes * g.fields:,} pages")
                        for g in SCAN_GROUPS
                    ],
                ],
            )))

    sizes = [a["size"] for a in survey.nwb]
    scans = sorted({dandi_scan(a) for a in survey.nwb}, key=lambda s: tuple(map(int, s.split("-"))))
    entries.append(
        (
            TreeNode("dandiset 000402", 0, 0, count=len(survey.nwb), subtitle="NWB files", status="added"),
            dict(
                title="dandiarchive.org/dandiset/000402",
                source="DANDI, s3://dandiarchive",
                accent=ACCENTS["source"],
                rows=[
                    Row("files", f"{len(survey.nwb)} NWB, subject {survey.nwb[0]['path'].split('/')[0].removeprefix('sub-')}"),
                    Row("size", f"{d.size_text(sum(sizes))} in all, {d.size_text(min(sizes))}–{d.size_text(max(sizes))} per file"),
                    Row("sessions", f"{len({s.split('-')[0] for s in scans})}, scans {', '.join(scans)}"),
                    Row("processed", "traces, ROI masks, behavior, stimulus, frame times", status="added", tag="ingest"),
                    Row("movies", "TwoPhotonSeries", status="removed", tag="TIFF used"),
                ],
            ),
        )
    )
    entries.append(
        (
            TreeNode("minnie65/", 0, 0, count=len(GOOGLE_RELEASES), subtitle="Google Cloud"),
            dict(
                title=GOOGLE_PREFIX,
                source="proofread segmentation releases beyond BossDB's v117",
                accent=ACCENTS["source"],
                rows=[Row(f"seg_m{r.removeprefix('v')}", f"release {r}") for r in GOOGLE_RELEASES],
            ),
        )
    )
    tree_figure(canvas, y + 20, entries)
    return canvas


# The mapping figure


def section(canvas: Canvas, y: float, title: str, subtitle: str) -> float:
    """A rule across the figure with a section title above it."""
    canvas.add(f'<line x1="24" y1="{y:.1f}" x2="{canvas.width - 24}" y2="{y:.1f}" stroke="{d.RULE}"/>', y)
    canvas.text(24, y + 22, title, size=14, weight="bold")
    canvas.text(24 + d.text_width(title, 14, bold=True) + 18, y + 22, subtitle, size=12, color=d.MUTED)
    return y + 40


def row_y(top: float, index: int, *, source: bool = True) -> float:
    """The y of the middle of row ``index`` in a panel drawn at ``top``."""
    return top + d.panel_height(0, source=source) - 6 + index * d.ROW_HEIGHT + d.ROW_HEIGHT / 2


LEFT = 24
SOURCE_WIDTH = 720
MIDDLE = 470
MIDDLE_WIDTH = 410
RIGHT = 960
STORE_X = 840
STORE_WIDTH = 536


def mapping_figure(survey: Survey) -> Canvas:
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Mapping", "proposed: each source becomes the store node beside it")
    y = d.legend(
        canvas,
        canvas.margin,
        y + 14,
        [
            (ACCENTS["source"], "source"),
            (ACCENTS["store"], "store"),
            ("arrow", "referenced or copied"),
            ("changed", "check in trial"),
            ("removed", "not referenced"),
        ],
    )

    def layer_lane(y: float, layer: Layer, store_path: str, width: float) -> float:
        """A layer's first scale as a cube on each side of a panel, then every scale as a table row."""
        scale = layer.scales[0]
        shape, chunks, offset = layer.shape(scale), layer.chunks(scale), layer.translation(scale)
        height = width * shape[1] / shape[2]
        source = CubeGeometry(LEFT, y + 10, width, height, 36)
        source_bottom = d.cube(
            canvas,
            source,
            f"source {layer.path}/{scale['key']}",
            [
                f"size   {dims(scale['size'])}  (x, y, z)",
                f"chunks {dims(scale['chunk_sizes'][0])}",
                f"{scale['encoding']}, {layer.sharding()}",
            ],
            accent=ACCENTS["source"],
            source="info, scale 0",
            label_width=440,
        )
        store = CubeGeometry(RIGHT, y + 10, width, height, 36)
        store_bottom = d.cube(
            canvas,
            store,
            f"store {store_path}/0",
            [
                f"shape  {dims(shape)}  (z, y, x)",
                f"chunks {dims(chunks)}",
                f"codec  {STORE_CODECS[scale['encoding']]}",
            ],
            accent=ACCENTS["store"],
            source="level 0",
            label_width=420,
        )
        panel_top = y + 10 + 36
        panel_bottom = d.panel(
            canvas,
            MIDDLE,
            panel_top,
            MIDDLE_WIDTH,
            "level 0",
            [
                Row("axes", "(z, y, x)  from  (x, y, z)"),
                Row("scale nm", dims(layer.resolution(scale))),
                Row("translation", f"{dims(offset)} nm"),
                Row("levels", f"{len(layer.scales)}, one per source scale"),
                Row("chunks", "one per source chunk"),
            ],
            accent=ACCENTS["highlight"],
            source="resolution, voxel_offset, size, chunk_sizes",
        )
        mid = panel_top + 60
        d.link(canvas, source.middle_right(), (MIDDLE, mid))
        d.link(canvas, (MIDDLE + MIDDLE_WIDTH, mid), store.middle_left(), label="referenced")
        y = max(source_bottom, store_bottom, panel_bottom) + 24
        cells = []
        for k, level in enumerate(layer.scales):
            listed = layer.listed.get(level["key"])
            cells.append(
                [
                    d.Cell((str(k),)),
                    d.Cell((f"{layer.path}/{level['key']}",)),
                    d.Cell((dims(layer.resolution(level)),)),
                    d.Cell((dims(layer.shape(level)),)),
                    d.Cell((dims(layer.chunks(level)),)),
                    d.Cell((level["encoding"],)),
                    d.Cell((f"{listed[0]:,} / {d.size_text(listed[1])}" if listed else "not surveyed",)),
                ]
            )
        header = ["level", "source scale", "nm (z,y,x)", "store shape", "chunks", "encoding", "objects / bytes"]
        widths = [
            max(d.text_width(text, 11.5, mono=True) for text in [title, *(row[j].lines[0] for row in cells)]) + 32
            for j, title in enumerate(header)
        ]
        return d.table(canvas, LEFT, y, widths, header, cells) + 30

    em = survey.layer(EM_EXAMPLE)
    y = section(canvas, y + 20, "EM", f"{em.path} → em/{em.top}")
    y = layer_lane(y, em, f"em/{em.top}", 280)

    seg = survey.layer(SEGMENTATION_EXAMPLE)
    y = section(canvas, y, "segmentation", f"{seg.path} → segmentation/{seg.path}, one of {sum(l.info['type'] == 'segmentation' for l in survey.layers)}")
    y = layer_lane(y, seg, f"segmentation/{seg.path}", 220)

    # Two-photon movie: one TIFF becomes one image per field.
    group = SCAN_GROUPS[0]
    pages = group.volumes * group.fields
    y = section(canvas, y, "two-photon movie", f"{EXAMPLE_TIFF} → two-photon/scans/{EXAMPLE_SCAN}/field-1…{group.fields}")
    frame_y, frame_x = group.frame
    source = CubeGeometry(LEFT, y + 10, 150, 150 * frame_y / frame_x, 90)
    source_bottom = d.cube(
        canvas,
        source,
        f"source {EXAMPLE_TIFF}",
        [f"shape  {dims((pages, frame_y, frame_x))}", "int16, uncompressed", "one strip per page"],
        accent=ACCENTS["source"],
        source="BossDB functional_data",
        label_width=420,
    )
    store = CubeGeometry(RIGHT, y + 10, 150, 150 * frame_y / frame_x, 90)
    store_bottom = d.cube(
        canvas,
        store,
        f"store two-photon/scans/{EXAMPLE_SCAN}/field-1",
        [f"shape  {dims((group.volumes, frame_y, frame_x))}", f"chunks {dims((1, frame_y, frame_x))}", "int16, uncompressed"],
        accent=ACCENTS["store"],
        source="one image per field",
        label_width=420,
    )
    field_rows = [Row(f"field-{n + 1}", f"pages {n}, {n + group.fields}, {n + 2 * group.fields}, …") for n in range(group.fields)]
    panel_top = y + 10
    panel_bottom = d.panel(
        canvas,
        MIDDLE,
        panel_top,
        MIDDLE_WIDTH,
        "page = fields × t + n − 1",
        [Row("axes", "t, y, x", status="changed", tag="check"), *field_rows],
        accent=ACCENTS["highlight"],
        source=f"{group.volumes:,} volumes × {group.fields} fields",
    )
    d.link(canvas, source.middle_right(), (MIDDLE, panel_top + 60))
    d.link(canvas, (MIDDLE + MIDDLE_WIDTH, panel_top + 60), store.middle_left(), label="referenced")
    y = max(source_bottom, store_bottom, panel_bottom) + 30

    # NWB: its processed groups and each field's imaging plane become attributes and a group.
    nwb = next((a for a in survey.nwb if dandi_scan(a) == EXAMPLE_SCAN), None)
    nwb_name = nwb["path"].rsplit("/", 1)[-1] if nwb else f"sub-17797_ses-{EXAMPLE_SCAN.replace('-', '-scan-')}_behavior+image+ophys.nwb"
    y = section(canvas, y, "NWB", f"{nwb_name} → two-photon/scans/{EXAMPLE_SCAN}")
    nwb_rows = [
        Row("acquisition/TwoPhotonSeries1…8", NWB_SERIES, status="removed", tag="TIFF used"),
        Row("general/optophysiology/ImagingPlane1…8", "grid_spacing, origin_coords, depth"),
        Row("processing/ophys/Fluorescence", ""),
        Row("processing/ophys/Deconvolved", ""),
        Row("processing/ophys/ImageSegmentation", "masks with pt_root_id"),
        Row("EyeTracking, PupilTracking", ""),
        Row("treadmill_velocity", ""),
        Row("stimulus ImageSeries", NWB_STIMULUS),
    ]
    source_top = y
    source_bottom = d.panel(
        canvas,
        LEFT,
        source_top,
        SOURCE_WIDTH,
        nwb_name,
        nwb_rows,
        accent=ACCENTS["source"],
        source=f"DANDI 000402, {d.size_text(nwb['size'])}" if nwb else "DANDI 000402",
        key_width=300,
    )
    field_top = y
    field_panel = [
        Row("pixel size", "grid_spacing, y and x"),
        Row("position", "origin_coords, in the scene"),
        Row("depth", "ImagingPlane depth"),
        Row("frame times", "timestamps, array beside the image"),
    ]
    field_bottom = d.panel(
        canvas, STORE_X, field_top, STORE_WIDTH, f"store two-photon/scans/{EXAMPLE_SCAN}/field-<n>", field_panel,
        accent=ACCENTS["store"], source="annotations",
    )
    processed_top = field_bottom + 16
    processed_rows = [
        Row("fluorescence", "referenced HDF5 chunks"),
        Row("deconvolved", "referenced HDF5 chunks"),
        Row("masks", "ROIs with EM identifiers"),
        Row("behavior", "eye, pupil, treadmill"),
        Row("stimulus", "referenced HDF5 chunks"),
    ]
    processed_bottom = d.panel(
        canvas, STORE_X, processed_top, STORE_WIDTH, f"store two-photon/scans/{EXAMPLE_SCAN}/processed", processed_rows,
        accent=ACCENTS["store"], source="processing/ophys, behavior, stimulus",
    )
    d.link(canvas, (LEFT + SOURCE_WIDTH, row_y(source_top, 1)), (STORE_X, row_y(field_top, 1)), label="attributes")
    for source_row, store_row in ((2, 0), (3, 1), (4, 2), (5, 3), (7, 4)):
        d.link(canvas, (LEFT + SOURCE_WIDTH, row_y(source_top, source_row)), (STORE_X, row_y(processed_top, store_row)))
    y = max(source_bottom, processed_bottom) + 30

    # CSV files: each listed directory of tables becomes one Iceberg table.
    files = survey.csv_files()
    y = section(canvas, y, "tables", "BossDB CSV files → Iceberg tables beside the store")
    table_top = y
    csv_rows = [Row(name, d.size_text(size)) for _, name, size in files] or [Row("no CSV listed", "", status="changed", tag="not surveyed")]
    source_bottom = d.panel(canvas, LEFT, table_top, SOURCE_WIDTH, "BossDB CSV files", csv_rows, accent=ACCENTS["source"], source="directories with files listed", key_width=300)
    store_rows = [Row(directory, f"from {name}") for directory, name, _ in files] or [Row("none", "")]
    store_bottom = d.panel(canvas, STORE_X, table_top, STORE_WIDTH, "Iceberg tables", store_rows, accent=ACCENTS["store"], source="beside the store, named for the source directory")
    for i in range(len(files)):
        d.link(canvas, (LEFT + SOURCE_WIDTH, row_y(table_top, i)), (STORE_X, row_y(table_top, i)), label="rows" if i == 0 else None)
    canvas.bottom = max(canvas.bottom, source_bottom, store_bottom)
    return canvas


# The store figure


def store_figure(survey: Survey) -> Canvas:
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Store", "proposed layout, nothing written yet")
    y = d.legend(canvas, canvas.margin, y + 14, [("added", "proposed"), ("changed", "to survey")])

    def layers_of(kind: str, top: str | None = None) -> list[Layer]:
        return [
            layer for layer in survey.layers
            if layer.info["type"] == kind and (top is None or layer.top == top) and (top is not None or layer.top != "microCT")
        ]

    scenes = [t for t in dict.fromkeys(layer.top for layer in survey.layers) if t.startswith("minnie")]
    scans = sum(g.scans for g in SCAN_GROUPS)
    fields = sum(g.scans * g.fields for g in SCAN_GROUPS)
    volumes = [g.volumes for g in SCAN_GROUPS]
    frame_rows = [g.frame[0] for g in SCAN_GROUPS]
    frame_columns = [g.frame[1] for g in SCAN_GROUPS]
    proposed = "added"

    def span_text(values: list[int]) -> str:
        low, high = min(values), max(values)
        return f"{low:,}" if low == high else f"{low:,}–{high:,}"

    entries: list[tuple[TreeNode, dict]] = [
        (
            TreeNode("/", 0, 0, subtitle="root", status=proposed),
            dict(
                title="/",
                accent=ACCENTS["store"],
                source="zarr.json",
                rows=[
                    Row("annotations", "title, citation, DOIs, license, sources, codecs used, release history"),
                    Row("ome", f"scenes {', '.join(scenes)}"),
                ],
            ),
        ),
        (
            TreeNode("em/<volume>", 0, 1, count=len(layers_of("image")), subtitle="image", status=proposed),
            dict(title="em/<volume>", accent=ACCENTS["store"], source="OME-Zarr multiscale image (z, y, x), nanometers", rows=layer_rows(layers_of("image"), detail=False)),
        ),
        (
            TreeNode("segmentation/<volume>/<layer>", 0, 1, count=len(layers_of("segmentation")), subtitle="image", status=proposed),
            dict(title="segmentation/<volume>/<layer>", accent=ACCENTS["store"], source="own multiscale image, placed in the volume's scene", rows=layer_rows(layers_of("segmentation"), detail=False)),
        ),
        (
            TreeNode("microct/<scan>", 0, 1, count=sum(layer.top == "microCT" for layer in survey.layers), subtitle="image", status=proposed),
            dict(title="microct/<scan>", accent=ACCENTS["store"], source="OME-Zarr multiscale image (z, y, x), nanometers", rows=layer_rows([l for l in survey.layers if l.top == "microCT"], detail=False)),
        ),
        (
            TreeNode("two-photon/structural/<stack>", 0, 1, subtitle="image", status=proposed),
            dict(title="two-photon/structural/<stack>", accent=ACCENTS["store"], source="vasculature stacks the fields register into", rows=[Row("stacks", "not surveyed", status="changed", tag="to survey")]),
        ),
        (
            TreeNode("two-photon/scans/<session>-<scan>", 0, 1, count=scans, subtitle="scene", status=proposed),
            dict(
                title="two-photon/scans/<session>-<scan>",
                accent=ACCENTS["store"],
                source="ome scene placing the scan's fields; annotations",
                rows=[
                    Row("fields", f"{span_text([g.fields for g in SCAN_GROUPS])} per scan, {fields} in all"),
                    Row("annotations", "rate, depths, nframes"),
                    *[Row(g.label, f"{g.volumes:,} volumes, {g.fields} fields, {g.rate_hz} Hz") for g in SCAN_GROUPS],
                ],
            ),
        ),
        (
            TreeNode("field-<n>", 0, 2, count=fields, subtitle="image", status=proposed),
            dict(
                title="two-photon/scans/<session>-<scan>/field-<n>",
                accent=ACCENTS["store"],
                source="one chunk per frame, referencing the BossDB TIFF",
                rows=[
                    Row("axes", "t, y, x  (second, micrometer, micrometer)"),
                    Row("dtype", "int16"),
                    Row("shape", f"({span_text(volumes)}, {span_text(frame_rows)}, {span_text(frame_columns)})"),
                    Row("chunks", f"(1, {span_text(frame_rows)}, {span_text(frame_columns)})"),
                    Row("pixel size", f"{', '.join(f'{p:g}' for p in sorted({g.pixel_um for g in SCAN_GROUPS}))} µm"),
                ],
            ),
        ),
        (
            TreeNode("processed", 0, 2, count=scans, subtitle="group", status=proposed),
            dict(
                title="two-photon/scans/<session>-<scan>/processed",
                accent=ACCENTS["store"],
                source=f"{len(survey.nwb)} DANDI NWB files, referenced",
                rows=[Row("arrays", "fluorescence, deconvolved, masks, behavior, stimulus")],
            ),
        ),
        (
            TreeNode("tables", 0, 0, count=len(TABLES), subtitle="Iceberg, beside the store", status=proposed),
            dict(
                title="tables",
                accent=ACCENTS["store"],
                source="Iceberg",
                rows=[Row(t, "meshes, skeletons, models, database dump, documentation" if t == "files" else "") for t in TABLES],
            ),
        ),
    ]
    tree_figure(canvas, y + 20, entries)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--survey", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    survey = read_survey(args.survey)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, figure in (("source", source_figure), ("mapping", mapping_figure), ("store", store_figure)):
        size = figure(survey).write(args.out / f"{name}.svg")
        print(f"{name}.svg  {size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
