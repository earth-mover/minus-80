"""Draw phase 1 of the MICrONS ingest: the source, how it maps onto the store, and the store.

    MINUS80_S3_ANONYMOUS=1 uv run --package microns-ingest python ingests/microns/diagrams.py \\
        --survey <survey.json> --layouts <layouts.json> --repo <icechunk repo dir>

Writes ``source.svg``, ``mapping.svg`` and ``store.svg`` into ``diagrams/`` beside this file.
Phase 1 references the two-photon functional scan TIFFs and takes per-field metadata from
DANDI 000402's NWB files; the source figure still shows the whole archive, with everything
else marked as not ingested. The values in the figures are read from three inputs:

- ``survey.json``, written by ``survey.py``: the bucket listing per directory (objects,
  bytes, extensions, and the file list of small directories) and the 19 NWB files' paths and
  sizes. A directory the survey did not list is drawn as "not surveyed", never as empty.
- ``layouts.json``, written by ``layouts.py``: one row per TIFF with its page count, page
  shape, dtype, first page offset, IFD size and tag count, and where the IFDs sit. It gives
  every scan's page count, which is also each export's chunk count.
- a store written by ``ingest.py``, opened read-only at tags ``v1`` and ``v2``: every
  attribute, shape, chunk shape and dtype of the example scan. The store may hold only some
  of the scans; the counts for all 19 come from the layouts.

The one fact in none of them is how a scan's page count splits into volumes and fields. It is
in the BossDB documentation PDF, so it is a constant below, and the figure stops if any scan's
pages differ from volumes × fields.

With ``--archive <samples.npz> --rois <roi_counts.json> --cave <dumps>``, it draws only
``archive.svg``: every part of the archive, grouped by host, with a small picture of each from
samples that ``archive_samples.py`` fetches and caches in the ``.npz`` (TensorStore, in the
ingest's ``archive`` dependency group, reads the precomputed volumes).

LAYOUTS.md explains the mapping; the figures carry no prose.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zarr

from minus80 import diagrams as d
from minus80.core.icechunk_io import open_repository
from minus80.diagrams import ACCENTS, Canvas, CubeGeometry, Row, TreeNode

OUT = Path(__file__).parent / "diagrams"
WIDTH = 1400

# Volumes and fields per scan, from BossDB's
# functional_data/two_photon_functional_scans/two_photon_functional_scans_technical_documentation_v2.pdf.
# Every TIFF holds volumes × fields pages, field-major within each volume.
VOLUMES_AND_FIELDS = ((40_000, 8), (35_112, 8), (50_000, 6), (57_000, 4))

SCANS_DIRECTORY = "functional_data/two_photon_functional_scans/"
EXAMPLE_SCAN = "4-7"
EXAMPLE_TIFF = "functional_scan_17797_4_7_v2.tif"
EXAMPLE_NWB = "sub-17797_ses-4-scan-7_behavior+image+ophys.nwb"
SCENE = "two-photon"

# Probed on 2026-11-12 in sub-17797_ses-4-scan-7_behavior+image+ophys.nwb (DANDI 000402):
# the stimulus ImageSeries, which is not referenced.
NWB_STIMULUS = "(381054, 144, 256, 3) uint8"

# The proofread segmentation's later releases are on Google Cloud only (MICrONS tutorial,
# "static repositories").
GOOGLE_RELEASES = ("v343", "v943", "v1300")
GOOGLE_PREFIX = "gs://iarpa_microns/minnie/minnie65/"

# The BossDB directory that holds the scans; it is shown as "not surveyed" when the survey
# did not list it.
FUNCTIONAL_DIRECTORY = "functional_data"

#: Swatch of a block that phase 1 leaves out.
NOT_INGESTED = d.RULE

FIELD_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#8e5bd4", "#c99a06", "#d6458f", "#2b9aa0", "#7a8b2a")


# Reading the inputs


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

    def kind(self) -> str:
        sharded = sum(1 for s in self.scales if s.get("sharding"))
        if sharded == len(self.scales):
            sharding = "sharded"
        else:
            sharding = "unsharded" if sharded == 0 else f"{sharded} of {len(self.scales)} scales sharded"
        encodings = ", ".join(dict.fromkeys(s["encoding"] for s in self.scales))
        return f"{self.info['type']} {self.info['data_type']}, {len(self.scales)} scales, {encodings}, {sharding}"

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
    """The survey's sections, with the layers worked out from them."""

    root: str
    bucket: str
    directories: dict[str, dict]
    layers: list[Layer]
    nwb: list[dict]

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


@dataclass(frozen=True)
class Tiff:
    """One TIFF's row of the layouts file, with the scan it belongs to."""

    name: str
    export: int
    scan: str
    pages: int
    frame: tuple[int, int]
    dtype: str
    first_offset: int
    entries: int
    stride: int
    first_directory: int
    pixels_end: int
    region_bytes: int
    contiguous: bool
    strips_per_page: int

    @property
    def volumes_and_fields(self) -> tuple[int, int]:
        (match,) = [(v, f) for v, f in VOLUMES_AND_FIELDS if v * f == self.pages]
        return match


TIFF_NAME = re.compile(r"^functional_scan_\d+_(?P<session>\d+)_(?P<scan>\d+)_v(?P<export>\d)\.tif$")


def read_layouts(path: Path) -> list[Tiff]:
    tiffs = []
    for row in json.loads(path.read_text()):
        m = TIFF_NAME.match(row["name"])
        tiffs.append(
            Tiff(
                name=row["name"],
                export=int(m["export"]),
                scan=f"{m['session']}-{m['scan']}",
                pages=row["pages"],
                frame=tuple(row["shape"][1:]),
                dtype=row["dtype"],
                first_offset=row["first_offset"],
                entries=row["entries"],
                stride=row["stride"],
                first_directory=row["first_directory"],
                pixels_end=row["pixels_end"],
                region_bytes=row["region_bytes"],
                contiguous=row["contiguous"],
                strips_per_page=len(row["page_bytes"]),
            )
        )
    for tiff in tiffs:
        tiff.volumes_and_fields  # raises when a scan's pages are not volumes × fields
    return tiffs


@dataclass
class Store:
    """What the example scan's groups hold at each tag, and the root's attributes."""

    root: dict
    scans_attributes: dict
    scan_attributes: dict
    scene: dict
    volume_times: dict
    field: dict
    array: dict[str, dict]
    fields: list[dict]


def read_store(path: Path) -> Store:
    repo = open_repository(str(path))
    root = {}
    array = {}
    for tag in ("v1", "v2"):
        store = repo.readonly_session(tag=tag).store
        group = zarr.open_group(store, mode="r")
        root[tag] = dict(group.attrs)
        scan = zarr.open_group(store, path=f"{SCENE}/{EXAMPLE_SCAN}", mode="r")
        image = scan["field-1/0"]
        array[tag] = {
            "shape": image.shape,
            "chunks": image.chunks,
            "dtype": str(image.dtype),
            "dims": tuple(image.metadata.dimension_names),
        }
    scans = zarr.open_group(store, path=SCENE, mode="r")
    times = scan["volume_times"]
    translations = {
        t["input"]["path"]: t["translation"] for t in scan.attrs["ome"]["scene"]["coordinateTransformations"]
    }
    fields = []
    for n in range(1, scan.attrs["scan"]["fields"] + 1):
        annotation = scan[f"field-{n}"].attrs["annotations"]
        fields.append(
            {
                "number": n,
                "center": annotation["center_um"],
                "depth": annotation["depth_um"],
                "pixel": annotation["pixel_um"],
                "translation": translations[f"field-{n}"],
            }
        )
    field = scan["field-1"].attrs
    return Store(
        root=root,
        scans_attributes=dict(scans.attrs),
        scan_attributes=dict(scan.attrs),
        scene=scan.attrs["ome"]["scene"],
        volume_times={
            "shape": times.shape,
            "chunks": times.chunks,
            "dtype": str(times.dtype),
            "attributes": sorted(times.attrs),
        },
        field={"ome": sorted(field["ome"]), "annotations": sorted(field["annotations"]), "tiff": sorted(field["tiff"])},
        array=array,
        fields=fields,
    )


@dataclass
class Facts:
    """Everything the three figures draw."""

    survey: Survey
    tiffs: list[Tiff]
    store: Store

    def exports(self, export: int) -> list[Tiff]:
        return [t for t in self.tiffs if t.export == export]

    @property
    def example(self) -> Tiff:
        return next(t for t in self.tiffs if t.name == EXAMPLE_TIFF)

    @property
    def scans(self) -> list[Tiff]:
        """One row per scan, from the v2 export."""
        return self.exports(2)

    @property
    def field_count(self) -> int:
        return sum(t.volumes_and_fields[1] for t in self.scans)

    @property
    def chunk_count(self) -> int:
        return sum(t.pages for t in self.scans)

    @property
    def nwb(self) -> dict:
        return next(a for a in self.survey.nwb if a["path"].endswith(EXAMPLE_NWB))


def span_text(values) -> str:
    low, high = min(values), max(values)
    return f"{low:,}" if low == high else f"{low:,}–{high:,}"


def shape_text(shape) -> str:
    return "(" + ", ".join(str(n) for n in shape) + ("," if len(shape) == 1 else "") + ")"


def one_value(values, what: str):
    """The single value of ``values``, or a stop when the exports disagree about ``what``."""
    unique = set(values)
    if len(unique) != 1:
        raise ValueError(f"expected one {what}, found {sorted(unique)}")
    return unique.pop()


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


# The source figure


def tiff_rows(facts: Facts) -> list[Row]:
    """The TIFF set's panel: what the listing and the layout trial found, per export."""
    listing = facts.survey.directories[SCANS_DIRECTORY]["files"]
    sizes = {n: sum(s for name, s in listing.items() if name.endswith(f"_v{n}.tif")) for n in (1, 2)}
    rows = [
        Row("files", f"{len(facts.tiffs)}: {len(facts.scans)} scans in each of exports v1 and v2"),
        Row("size", f"{sizes[1] / 1e12:.2f} TB in v1, {sizes[2] / 1e12:.2f} TB in v2"),
    ]
    for export in (2, 1):
        group = facts.exports(export)
        dtype = one_value((t.dtype for t in group), "dtype")
        offset = one_value((t.first_offset for t in group), "first page offset")
        entries = one_value((t.entries for t in group), "tag count")
        stride = one_value((t.stride for t in group), "IFD size")
        rows.append(
            Row(f"v{export}", f"{dtype}, first page at byte {offset}, IFDs of {entries} tags ({stride} bytes)",
                status="added", tag="ingest")
        )
    pages = Counter(((t.pages // t.volumes_and_fields[1], t.volumes_and_fields[1], t.frame) for t in facts.scans))
    for (volumes, fields, frame), n in sorted(pages.items(), key=lambda kv: -kv[1]):
        rows.append(
            Row(f"{n} scan{'s' if n > 1 else ''}", f"{volumes:,} volumes × {fields} fields, {frame[0]} × {frame[1]} pages")
        )
    if all(t.contiguous for t in facts.tiffs) and all(t.strips_per_page == 1 for t in facts.tiffs):
        rows.append(Row("pixels", "every file's pages contiguous, one uncompressed strip per page"))
    first = one_value((t.first_directory for t in facts.tiffs), "first IFD offset")
    regions = [t.region_bytes / 2**20 for t in facts.tiffs]
    rows.append(
        Row("IFDs", f"first at byte {first}, the rest packed after the pixels, "
                    f"{min(regions):.0f}–{max(regions):.0f} MiB per file")
    )
    return rows


def source_figure(facts: Facts) -> Canvas:
    survey = facts.survey
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Source", f"{survey.bucket}/{survey.root}, DANDI 000402, gs://iarpa_microns")
    y = d.legend(
        canvas,
        canvas.margin,
        y + 14,
        [(ACCENTS["source"], "source"), ("added", "ingested in phase 1"), (NOT_INGESTED, "not ingested")],
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
            rows = []
            for layer in layers:
                rows += [Row(layer.name, layer.kind()), Row("listed", layer.listing())]
            count, subtitle = len(layers), "Neuroglancer layers, not ingested"
        elif groups:
            rows = []
            for group, paths in groups.items():
                entries_here = [survey.directories[p] for p in paths]
                extensions = sum((Counter(e["extensions"]) for e in entries_here), Counter())
                size = d.size_text(sum(e["bytes"] for e in entries_here))
                many = f"{len(paths)} directories, " if len(paths) > 1 else ""
                rows.append(Row(group, f"{many}{sum(e['objects'] for e in entries_here):,} objects, {size}, {extension_text(extensions)}"))
            count = len(groups)
            subtitle = "only the scan TIFFs ingested" if top == FUNCTIONAL_DIRECTORY else "not ingested"
        else:
            rows = [Row("listing", "none", status="changed", tag="not surveyed")]
            count, subtitle = None, "not ingested"
        entries.append(
            (
                TreeNode(f"{top}/", 0, 1, count, subtitle),
                dict(title=f"{top}/", source="BossDB", accent=ACCENTS["source"], rows=rows),
            )
        )
        if top == FUNCTIONAL_DIRECTORY:
            entries.append(
                (
                    TreeNode("two_photon_functional_scans/", 0, 2, count=len(facts.tiffs), subtitle="TIFFs", status="added"),
                    dict(
                        title="two-photon functional scan TIFFs",
                        source=f"{SCANS_DIRECTORY} listing, layout trial",
                        accent=ACCENTS["source"],
                        rows=tiff_rows(facts),
                    ),
                )
            )

    sizes = [a["size"] for a in survey.nwb]
    scans = sorted({re.search(r"ses-(\d+-scan-\d+)", a["path"])[1].replace("-scan-", "-") for a in survey.nwb},
                   key=lambda s: tuple(map(int, s.split("-"))))
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
                    Row("field metadata", "ImagingPlane<n>, TwoPhotonSeries<n> timestamps", status="added", tag="ingest"),
                    Row("movies", "TwoPhotonSeries data", status="removed", tag="TIFF used"),
                    Row("processed", "traces, ROI masks, behavior, stimulus, frame times"),
                    Row("stimulus", NWB_STIMULUS),
                ],
            ),
        )
    )
    entries.append(
        (
            TreeNode("minnie65/", 0, 0, count=len(GOOGLE_RELEASES), subtitle="Google Cloud, not ingested"),
            dict(
                title=GOOGLE_PREFIX,
                source="proofread segmentation releases beyond BossDB's",
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


def row_y(top: float, index: int) -> float:
    """The y of the middle of row ``index`` in a panel with a source line drawn at ``top``."""
    return top + d.panel_height(0, source=True) - 6 + index * d.ROW_HEIGHT + d.ROW_HEIGHT / 2


LEFT = 24
SOURCE_WIDTH = 720
STORE_X = 840
STORE_WIDTH = 536

BOX = 36
PITCH = 40
BOX_HEIGHT = 38
FIELD_PITCH = 168


def pages_strip(canvas: Canvas, y: float, fields: int, volumes: int) -> float:
    """The first ``volumes`` volumes of pages as boxes, each colored by its field. Returns the strip's bottom."""
    formula = f"page = volume × {fields} + (field − 1)"
    canvas.text(LEFT, y + 14, formula, size=13, weight="bold", mono=True)
    bracket = y + 36
    for v in range(volumes):
        x0 = LEFT + v * fields * PITCH
        x1 = x0 + fields * PITCH - (PITCH - BOX)
        canvas.add(
            f'<path d="M {x0:.1f} {bracket + 8:.1f} V {bracket + 3:.1f} H {x1:.1f} V {bracket + 8:.1f}" '
            f'fill="none" stroke="{d.MUTED}" stroke-width="1.2"/>',
            bracket + 8,
        )
        canvas.text((x0 + x1) / 2, bracket - 3, f"volume {v}", size=11, color=d.MUTED, anchor="middle")
    top = bracket + 16
    for page in range(volumes * fields):
        field = page % fields
        x = LEFT + page * PITCH
        color = FIELD_COLORS[field % len(FIELD_COLORS)]
        canvas.add(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{BOX}" height="{BOX_HEIGHT}" rx="3" '
            f'fill="{color}" fill-opacity="0.18" stroke="{color}" stroke-width="1.2"/>',
            top + BOX_HEIGHT,
        )
        canvas.text(x + BOX / 2, top + 14, str(page), size=10, color=d.MUTED, mono=True, anchor="middle")
        canvas.text(x + BOX / 2, top + 30, f"f{field + 1}", size=12, weight="bold", mono=True, anchor="middle")
    return top + BOX_HEIGHT


def mapping_figure(facts: Facts) -> Canvas:
    store = facts.store
    example = facts.example
    volumes, fields = example.volumes_and_fields
    rows, columns = example.frame
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Mapping", f"{EXAMPLE_TIFF} and {EXAMPLE_NWB} → {SCENE}/{EXAMPLE_SCAN}")
    y = d.legend(
        canvas,
        canvas.margin,
        y + 14,
        [(ACCENTS["source"], "source"), (ACCENTS["store"], "store"), ("arrow", "referenced or copied"), ("removed", "not referenced")],
    )

    # The pages of one TIFF, and the field images that take them.
    y = section(canvas, y + 20, "pages to fields", f"{example.pages:,} pages of {rows} × {columns}, first 3 volumes drawn")
    strip_top = y + 6
    strip_bottom = pages_strip(canvas, strip_top, fields, 3)
    tiff_x = LEFT + 3 * fields * PITCH + 100
    tiff = CubeGeometry(tiff_x, y + 6, 64, 64 * rows / columns, 40)
    tiff_bottom = d.cube(
        canvas,
        tiff,
        EXAMPLE_TIFF,
        [f"shape  {shape_text((example.pages, rows, columns))}", f"{example.dtype}, uncompressed", "one strip per page"],
        accent=ACCENTS["source"],
        source="BossDB, v2 export",
        label_width=WIDTH - 24 - tiff_x,
    )
    d.link(canvas, tiff.middle_left(), (LEFT + 3 * fields * PITCH - 2, strip_top + 52 + BOX_HEIGHT / 2), label="pages")
    cubes_top = max(strip_bottom, tiff_bottom) + 100
    array = store.array["v2"]
    shape, chunks = array["shape"], array["chunks"]
    cube_bottom = 0.0
    for n in range(1, fields + 1):
        x = LEFT + (n - 1) * FIELD_PITCH
        color = FIELD_COLORS[(n - 1) % len(FIELD_COLORS)]
        geometry = CubeGeometry(x, cubes_top, 56, 56 * rows / columns, 24)
        cube_bottom = max(
            cube_bottom,
            d.cube(
                canvas,
                geometry,
                f"field-{n}/0",
                [shape_text(shape), f"chunk {shape_text(chunks)}"],
                accent=ACCENTS["store"],
                label_width=FIELD_PITCH - 8,
            ),
        )
        canvas.add(
            f'<rect x="{x:.1f}" y="{geometry.face_bottom + 5:.1f}" width="56" height="4" fill="{color}"/>',
            geometry.face_bottom + 9,
        )
        d.link(
            canvas,
            (LEFT + (n - 1) * PITCH + BOX / 2, strip_bottom),
            (x + geometry.depth + geometry.width / 2, cubes_top),
            vertical=True,
            color=color,
        )
    y = cube_bottom + 18
    canvas.text(LEFT, y, f"store {SCENE}/{EXAMPLE_SCAN}/field-<n>/0: shape and chunks in ({', '.join(array['dims'])}); "
                         f"field n is every {fields}th page from n − 1", size=11.5, color=d.MUTED)
    y += 28

    # What the NWB file adds.
    y = section(canvas, y, "NWB metadata", f"{EXAMPLE_NWB} → {SCENE}/{EXAMPLE_SCAN}")
    example_field = store.fields[0]
    rate = store.scan_attributes["scan"]["rate_hz"]
    times = store.volume_times
    left_rows = [
        Row("ImagingPlane<n>/origin_coords", "(x, y, depth) of the field center, meters"),
        Row("ImagingPlane<n>/grid_spacing", f"{example_field['pixel']:g} µm, meters in the file"),
        Row("TwoPhotonSeries<n>/timestamps", f"{shape_text(times['shape'])} seconds"),
        Row("TwoPhotonSeries<n>/description", f"\"… at {rate} Hz …\""),
        Row("TwoPhotonSeries<n>/data", f"{shape_text((volumes, columns, rows))}", status="removed", tag="TIFF used"),
    ]
    right_rows = [
        Row("scene translation", "(t, z, y, x) per field, in motor"),
        Row("field scale y, x", f"{example_field['pixel']:g} µm"),
        Row("volume_times", f"{shape_text(times['shape'])} {times['dtype']}"),
        Row("field scale t", f"1 / {rate} Hz = {1 / rate:.4f} s"),
    ]
    top = y
    left_bottom = d.panel(canvas, LEFT, top, SOURCE_WIDTH, EXAMPLE_NWB, left_rows, accent=ACCENTS["source"],
                          source=f"DANDI 000402, {d.size_text(facts.nwb['size'])}", key_width=250)
    right_bottom = d.panel(canvas, STORE_X, top, STORE_WIDTH, f"{SCENE}/{EXAMPLE_SCAN}", right_rows,
                           accent=ACCENTS["store"], source="scene and attributes", key_width=170)
    for index in range(len(right_rows)):
        d.link(canvas, (LEFT + SOURCE_WIDTH, row_y(top, index)), (STORE_X, row_y(top, index)))
    y = max(left_bottom, right_bottom) + 20
    header = ["field", "center x, y (µm)", "depth (µm)", "scene translation (t, z, y, x)"]
    cells = [
        [
            d.Cell((f"field-{f['number']}",)),
            d.Cell((f"{f['center']['x']:g}, {f['center']['y']:g}",)),
            d.Cell((f"{f['depth']:g}",)),
            d.Cell((", ".join(f"{v:g}" for v in f["translation"]),)),
        ]
        for f in store.fields
    ]
    y = d.table(canvas, LEFT, y, [140, 220, 140, 380], header, cells, row_height=26) + 30

    # The two exports.
    y = section(canvas, y, "commits", "same pages in both: chunk (v, 0, 0, n − 1) is page v × "
                f"{fields} + n − 1")
    sizes = facts.survey.directories[SCANS_DIRECTORY]["files"]
    for export in (1, 2):
        tiff_row = next(t for t in facts.tiffs if t.scan == EXAMPLE_SCAN and t.export == export)
        tag = f"v{export}"
        top = y
        source_bottom = d.panel(
            canvas, LEFT, top, SOURCE_WIDTH, tiff_row.name,
            [
                Row("dtype", tiff_row.dtype),
                Row("first page", f"byte {tiff_row.first_offset}"),
                Row("IFDs", f"{tiff_row.entries} tags, {tiff_row.stride} bytes each"),
            ],
            accent=ACCENTS["source"], source=f"BossDB, {d.size_text(sizes[tiff_row.name])}",
            key_width=120,
        )
        export_attrs = store.root[tag]["tiff_export"]
        store_bottom = d.panel(
            canvas, STORE_X, top, STORE_WIDTH, f"commit {tag}, tag {tag}",
            [
                Row("dtype", store.array[tag]["dtype"]),
                Row("exported", export_attrs["date"]),
                Row("chunks", f"{facts.chunk_count:,} in all, {tiff_row.pages:,} in this scan"),
            ],
            accent=ACCENTS["store"], source="field arrays reference the TIFF", key_width=120,
        )
        d.link(canvas, (LEFT + SOURCE_WIDTH, row_y(top, 1)), (STORE_X, row_y(top, 1)), label="referenced")
        y = max(source_bottom, store_bottom) + 16
    canvas.bottom = max(canvas.bottom, y)
    return canvas


# The store figure


def store_figure(facts: Facts) -> Canvas:
    store = facts.store
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Store", "OME-Zarr 0.6, one scene per scan, two commits")
    y = d.legend(canvas, canvas.margin, y + 14, [("added", "written by ingest.py")])

    scans = len(facts.scans)
    fields = facts.field_count
    volumes = [t.pages // t.volumes_and_fields[1] for t in facts.scans]
    frame_rows = [t.frame[0] for t in facts.scans]
    frame_columns = [t.frame[1] for t in facts.scans]
    scan = store.scan_attributes["scan"]
    root = store.root["v2"]
    proposed = "added"

    entries: list[tuple[TreeNode, dict]] = [
        (
            TreeNode("/", 0, 0, subtitle="root", status=proposed),
            dict(
                title="/",
                accent=ACCENTS["store"],
                source="zarr.json",
                rows=[
                    Row("title", root["title"]),
                    Row("citation", f"{len(root['citation'])} references"),
                    Row("license", root["license"]),
                    Row("sources", ", ".join(root["sources"])),
                    Row("tiff_export", "; ".join(
                        f"{tag}: {store.root[tag]['tiff_export']['dtype']}, {store.root[tag]['tiff_export']['date']}"
                        for tag in ("v1", "v2")) + " (one per commit)"),
                    Row("layout", "one scene per scan under two-photon"),
                ],
            ),
        ),
        (
            TreeNode(SCENE, 0, 1, count=scans, subtitle="group", status=proposed),
            dict(
                title=SCENE,
                accent=ACCENTS["store"],
                source="zarr.json",
                rows=[
                    Row("description", "one scene per scan, <session>-<scan>; no OME-Zarr metadata"),
                    Row("scans", f"{scans} entries: fields, volumes, rate_hz"),
                ],
            ),
        ),
        (
            TreeNode("<session>-<scan>", 0, 2, count=scans, subtitle="scene", status=proposed),
            dict(
                title=f"{SCENE}/<session>-<scan>",
                accent=ACCENTS["store"],
                source=f"zarr.json, example {EXAMPLE_SCAN}",
                rows=[
                    Row("scene", f"version 0.6, coordinate system \"{store.scene['coordinateSystems'][0]['name']}\" "
                               f"({', '.join(a['name'] for a in store.scene['coordinateSystems'][0]['axes'])})"),
                    Row("translations", f"one per field image, by path; {len(store.fields)} in {EXAMPLE_SCAN}"),
                    Row("scan", ", ".join(scan)),
                ],
            ),
        ),
        (
            TreeNode("volume_times", 0, 3, count=scans, subtitle="array", status=proposed),
            dict(
                title=f"{SCENE}/<session>-<scan>/volume_times",
                accent=ACCENTS["store"],
                source=f"{EXAMPLE_SCAN}: {shape_text(store.volume_times['shape'])}",
                rows=[
                    Row("dtype", store.volume_times["dtype"]),
                    Row("shape", f"(volumes,), volumes {span_text(volumes)}"),
                    Row("chunks", "one chunk"),
                    Row("attributes", ", ".join(store.volume_times["attributes"])),
                ],
            ),
        ),
        (
            TreeNode("field-<n>", 0, 3, count=fields, subtitle="image", status=proposed),
            dict(
                title=f"{SCENE}/<session>-<scan>/field-<n>",
                accent=ACCENTS["store"],
                source="zarr.json",
                rows=[
                    Row("ome", "multiscales, one level; scale (t, z, y, x)"),
                    Row("annotations", ", ".join(store.field["annotations"][:7]) + f", … ({len(store.field['annotations'])} keys)"),
                    Row("tiff", ", ".join(store.field["tiff"])),
                ],
            ),
        ),
        (
            TreeNode("0", 0, 4, count=fields, subtitle="array", status=proposed),
            dict(
                title=f"{SCENE}/<session>-<scan>/field-<n>/0",
                accent=ACCENTS["store"],
                source=f"{facts.chunk_count:,} chunks per export, one per TIFF page",
                rows=[
                    Row("dimensions", f"({', '.join(store.array['v2']['dims'])})"),
                    Row("shape", f"({span_text(volumes)}, 1, {span_text(frame_rows)}, {span_text(frame_columns)})"),
                    Row("chunks", f"(1, 1, {span_text(frame_rows)}, {span_text(frame_columns)})"),
                    Row("dtype", f"{store.array['v1']['dtype']} at v1, {store.array['v2']['dtype']} at v2"),
                ],
            ),
        ),
        (
            TreeNode("tags", 0, 0, count=2, subtitle="v1, v2", status=proposed),
            dict(
                title="tags",
                accent=ACCENTS["store"],
                source="icechunk",
                rows=[
                    Row("v1", "first commit: chunks reference the v1 TIFFs"),
                    Row("v2", "second commit: same fields, chunks reference the v2 TIFFs"),
                ],
            ),
        ),
    ]
    tree_figure(canvas, y + 20, entries)
    return canvas


# The archive map
#
# One figure of everything MICrONS minnie publishes, grouped by host, with a
# small picture of each part drawn from samples read by archive_samples.py.
# Sizes come from the survey's listing of the BossDB bucket where it listed a
# directory, from the precomputed info files for chunk counts, and otherwise
# from the constants below with their sources. Each card says whether the
# store holds the part, and where, or why not.

#: Sizes BossDB's project page states for the precomputed layers, whose shard
#: directories the survey did not list (https://bossdb.org/project/microns-minnie).
BOSSDB_PAGE_SIZES = {"minnie65/em": "117 TB", "minnie35/em": "55 TB", "minnie65/seg": "12 TB",
                     "minnie65/ws": "42 TB", "minnie35/seg": "10 TB", "minnie35/ws": "22 TB"}

LICENSE = "CC BY 4.0"
IN_STORE, LEFT_OUT = "added", "removed"


@dataclass(frozen=True)
class Part:
    """One part of the archive as the map shows it."""

    title: str
    what: str
    where: str
    form: str
    size: str
    store: str
    held: bool
    pictures: tuple = ()


def prefix_total(survey: Survey, prefix: str) -> tuple[int, int]:
    """Objects and bytes the survey listed under ``prefix``."""
    objects = size = 0
    for path, entry in survey.directories.items():
        if path.startswith(prefix):
            objects += entry["objects"]
            size += entry["bytes"]
    return objects, size


def listed(survey: Survey, prefix: str) -> str:
    objects, size = prefix_total(survey, prefix)
    return f"{objects:,} objects, {d.size_text(size)}"


def full_resolution_chunks(survey: Survey, layer: str) -> int:
    scale = survey_layer(survey, layer).scales[0]
    return int(np.prod([-(-n // c) for n, c in zip(scale["size"], scale["chunk_sizes"][0])]))


def survey_layer(survey: Survey, path: str) -> Layer:
    (layer,) = [layer for layer in survey.layers if layer.path == path]
    return layer


def label_colors(labels: np.ndarray, seed: int = 3) -> np.ndarray:
    """A segmentation as RGB, one random color per label and black for label 0."""
    values, inverse = np.unique(labels, return_inverse=True)
    palette = np.random.default_rng(seed).integers(60, 256, size=(len(values), 3), dtype=np.uint8)
    palette[values == 0] = 0
    return palette[inverse].reshape(*labels.shape, 3)


def archive_pictures(samples: dict[str, np.ndarray]) -> dict[str, tuple[bytes, str]]:
    """Each picture's image bytes and caption, keyed by what it shows."""
    thumb = lambda array, width=220: d.thumbnail(array, width, quality=72)
    trace = samples["trace"]
    times = samples["trace_times"]
    pictures = {
        "frame": (thumb(samples["two_photon"].astype("float32").mean(axis=0), 120),
                  "4-7 field 4, 16 frames"),
        "stack": (thumb(samples["stack"], 160), "page 335 of 670"),
        "stimulus": (d.thumbnail(samples["stimulus_frame"], 220, quality=72), "stimulus, frame 200,000"),
        "masks": (thumb(samples["masks_max"], 120), "field 4 masks"),
        "trace": (d.plot(lambda ax: ax.plot(times, trace, lw=0.3, color="#2a78d6"), width=320, height=90),
                  "ROI 23 fluorescence, whole scan"),
        "behavior": (d.plot(lambda ax: ax.plot(samples["treadmill"], lw=0.3, color="#1baf7a"), width=320, height=70),
                     "treadmill velocity, first 60,000 samples"),
        "twin": (d.plot(lambda ax: ax.plot(samples["digital_twin"], lw=0.4, color="#8e5bd4"), width=320, height=90),
                 "unit 0, first 3,000 model responses"),
        "em_8nm": (thumb(samples["em_8nm"], 150), "8 nm, 2 µm"),
        "em_32nm": (thumb(samples["em_32nm"], 150), "32 nm, 8 µm"),
        "em_128nm": (thumb(samples["em_128nm"], 150), "128 nm, 33 µm"),
        "segmentation": (d.thumbnail(label_colors(samples["seg_64nm"]), 150, quality=80),
                         "v1300, 64 nm, 16 µm"),
        "microct": (thumb(samples["microct"], 150), "mid slice, 3.76 µm"),
        "viewer": (d.thumbnail(label_colors(samples["field_seg_mask"].T), 120, quality=80),
                   "4-7 field 4 labels"),
    }
    return pictures


def cave_plot(cave_rows: list[tuple[str, str, float]]) -> bytes:
    """Per dumped version after a table's first, the largest share of its root IDs changed since the last dump."""
    versions = list(dict.fromkeys(v for v, _, share in cave_rows if share is not None))
    peak = [max(share for v2, _, share in cave_rows if v2 == v and share is not None) for v in versions]

    def draw(ax):
        ax.bar(range(len(versions)), peak, color="#eb6834")
        for i, (v, share) in enumerate(zip(versions, peak)):
            ax.text(i, share, f"{v}\n{share:.0f}%", ha="center", va="bottom", fontsize=6)
        ax.set_ylim(0, max(peak) * 1.6)
    return d.plot(draw, width=320, height=100)


def archive_parts(survey: Survey, rois: dict, cave_rows: list, samples) -> list[tuple[str, str, list[Part]]]:
    """The hosts, each with its line of description and its parts."""
    nwb_total = sum(a["size"] for a in survey.nwb)
    em65 = survey_layer(survey, "minnie65/em")
    rois_total = sum(sum(c[0] for c in fields) for fields in rois.values())
    tables = sorted({t for _, t, _ in cave_rows})
    bossdb = [
        Part("two-photon movies, v1 and v2",
             "the calcium-imaging movies: 19 scans, every field's frames interleaved, 2021 and 2023 releases",
             "functional_data/two_photon_functional_scans/", "uncompressed BigTIFF, one strip per frame",
             listed(survey, "functional_data/two_photon_functional_scans/"),
             "phase 1: two-photon/<scan>/field-<n>, tags v1, v2", True, ("frame",)),
        Part("platinum_camovies", "byte copies of the v1 movies, plus scans 5-4, 5-5 and 8-4",
             "functional_data/platinum_camovies/", "uncompressed BigTIFF",
             listed(survey, "functional_data/platinum_camovies/"),
             "left out: copies; the extra scans have no metadata", False),
        Part("structural stack", "a two-photon volume of the imaged cortex, blood vessels labeled red",
             "functional_data/two_photon_structural_stacks/", "uncompressed float32 TIFF, 670 and 1,340 pages",
             listed(survey, "functional_data/two_photon_structural_stacks/"),
             "left out: later phase, with the field registrations", False, ("stack",)),
        Part("stimulus movies", "the movies the mouse watched, per scan, v3 and v4",
             "functional_data/stimulus_movies/, platinum_stimulus_movies/", "AVI of PNG frames (MPNG), MP4",
             f"{listed(survey, 'functional_data/stimulus_movies/')}; "
             f"{listed(survey, 'functional_data/platinum_stimulus_movies/')}",
             "left out: the NWB file holds the v4 movie", False),
        Part("digital twin", "a network model of the recorded neurons: responses, tuning, readout, v1 and v2",
             "functional_data/digital_twin_properties/, foundation_model/", "netCDF (v1); .npy and CSV (v2)",
             f"{listed(survey, 'functional_data/digital_twin_properties/')}; model "
             f"{listed(survey, 'functional_data/foundation_model/')}",
             "left out: later phase, both versions virtual", False, ("twin",)),
        Part("DataJoint database", "Baylor's processing database: deconvolved activity, field and stack registration",
             "functional_data/two_photon_processed_data_and_metadata/", "MySQL dump and Docker images, v7 and v8",
             listed(survey, "functional_data/two_photon_processed_data_and_metadata/"),
             "left out: needs a MySQL restore", False),
        Part("EM, minnie65", "electron microscopy of the same cortex, 65% of the reconstructed sections",
             "minnie65/em/", f"Neuroglancer precomputed, {len(em65.scales)} scales, sharded",
             f"{BOSSDB_PAGE_SIZES['minnie65/em']}; {full_resolution_chunks(survey, 'minnie65/em'):,} chunks at 8 nm",
             "left out: already cloud-optimized", False, ("em_8nm", "em_32nm", "em_128nm")),
        Part("EM, minnie35", "the other 35% of the sections, before the knife change, in the same frame",
             "minnie35/em/", "Neuroglancer precomputed, sharded",
             f"{BOSSDB_PAGE_SIZES['minnie35/em']}; {full_resolution_chunks(survey, 'minnie35/em'):,} chunks at 8 nm",
             "left out: already cloud-optimized", False),
        Part("segmentation v117 and its layers", "every EM voxel's cell, the first release; watershed, clefts, nuclei",
             "minnie65/{seg,autoseg,ws,clefts,nuclei}/, minnie35/", "precomputed compressed_segmentation",
             f"seg {BOSSDB_PAGE_SIZES['minnie65/seg']}, ws {BOSSDB_PAGE_SIZES['minnie65/ws']}; "
             f"{full_resolution_chunks(survey, 'minnie65/seg'):,} chunks at 8 nm",
             "left out: hundreds of millions of chunks per release", False),
        Part("microCT", "an X-ray volume of the same tissue block at 0.94 µm, in its own frame",
             "microCT/17797_1R_0.94um/", "Neuroglancer precomputed, raw, 4 scales",
             listed(survey, "microCT/"), "left out: no transform into EM space", False, ("microct",)),
        Part("tables", "synapses, nuclei, cell types, proofreading status, the 2021 2P-EM matches",
             "minnie65/synapse_graph/, nucleus_*, cell_types/, proofreading_status/, functional_coregistration/",
             "CSV", f"synapse graph {listed(survey, 'minnie65/synapse_graph/')}",
             "left out: Iceberg tables later", False),
        Part("viewer copies", "copies of the processed data for Neuroglancer: masks as labels, summaries, series",
             "functional_data/field_seg_masks/, field_summaries/, functional_series/, stimulus_n5/, stack_rgb/",
             "precomputed and N5",
             f"masks {listed(survey, 'functional_data/field_seg_masks/')}",
             "left out: copies of the NWB data", False, ("viewer",)),
    ]
    dandi = [
        Part("NWB files, one per scan", "the movies again, and what processing found: cell masks, traces, behavior, stimulus, trials",
             "dandiset 000402, version 0.230307.2132", "NWB (HDF5), gzip chunks",
             f"{len(survey.nwb)} files, {d.size_text(nwb_total)}; {rois_total:,} cells in 144 fields",
             "phase 2: two-photon/<scan>/nwb, rois-<n>, units", True,
             ("masks", "trace", "behavior", "stimulus")),
    ]
    google = [
        Part("segmentations v343, v943, v1300", "every EM voxel's cell in three proofread releases",
             "gs://iarpa_microns/minnie/minnie65/seg_m{343,943,1300}/", "precomputed compressed_segmentation, sharded",
             "about 627 million chunks per release at 8 nm",
             "left out: hundreds of millions of chunks per release", False, ("segmentation",)),
        Part("EM copy and derived layers", "a raw-encoded copy of the minnie65 EM, CLAHE versions, embeddings",
             "gs://iarpa_microns/minnie/minnie65/em/ and 14 more prefixes", "Neuroglancer precomputed",
             "not listed", "left out: a copy of BossDB's EM", False),
    ]
    cave = [
        Part("two-photon to EM match tables", "which EM cell each imaged cell is, per CAVE release",
             "gs://mat_dbs/public/minnie65_phase3_v1/v<release>/", "whole-file gzip CSV",
             f"{len(tables)} tables in 8 releases; 200 to 84,233 rows",
             "phase 2: two-photon/<scan>/units/em, tags cave-v<release>", True, ("cave",)),
        Part("synapse and other annotation tables", "every synapse, cell types, nuclei, proofreading, per release",
             "gs://mat_dbs/public/minnie65_phase3_v1/ and the live CAVE service", "whole-file gzip CSV; a service",
             "up to 22.9 GB per table", "left out: Iceberg tables later", False),
    ]
    return [
        ("BossDB", "s3://bossdb-open-data/iarpa_microns/minnie/, us-east-1, anonymous; run by JHU APL", bossdb),
        ("DANDI", "s3://dandiarchive/ (dandiset 000402), us-east-2, anonymous", dandi),
        ("Google Cloud", "gs://iarpa_microns/minnie/, anonymous", google),
        ("CAVE", "the MICrONS annotation service; static dumps in gs://mat_dbs/, anonymous", cave),
    ]


def archive_figure(survey: Survey, rois: dict, cave_rows: list, samples: dict[str, np.ndarray]) -> Canvas:
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "MICrONS minnie: everything the archive publishes",
                  "one mouse's visual cortex, imaged live with two-photon microscopy and then by electron "
                  "microscopy; grouped by host") + 24
    y = d.legend(canvas, LEFT, y, [(IN_STORE, "in the store"), (LEFT_OUT, "left out")]) + 20
    pictures = archive_pictures(samples)
    pictures["cave"] = (cave_plot(cave_rows), "largest share of root IDs changed, per release")
    panel_width, picture_x = 760, LEFT + 790
    for host, line, parts in archive_parts(survey, rois, cave_rows, samples):
        canvas.add(f'<rect x="{LEFT}" y="{y:.1f}" width="{WIDTH - 2 * LEFT}" height="34" rx="4" '
                   f'fill="{d.FACE_FILL}"/>', y + 34)
        canvas.text(LEFT + 12, y + 23, host, size=17, weight="bold")
        canvas.text(LEFT + 12 + d.text_width(host, 17, bold=True) + 16, y + 22, line, size=12,
                    color=d.MUTED, mono=True)
        y += 50
        for part in parts:
            rows = [Row("what", part.what), Row("where", part.where), Row("format", part.form),
                    Row("size", part.size), Row("license", LICENSE),
                    Row("store", part.store, status=IN_STORE if part.held else LEFT_OUT,
                        tag="IN STORE" if part.held else "LEFT OUT")]
            bottom = d.panel(canvas, LEFT, y, panel_width, part.title, rows,
                             accent=ACCENTS["store"] if part.held else ACCENTS["source"])
            x, top, row_bottom = picture_x, y, y
            for key in part.pictures:
                image, caption = pictures[key]
                width = {"trace": 300, "behavior": 300, "twin": 300, "cave": 300, "stimulus": 220, "stack": 160,
                         "frame": 120, "masks": 120, "viewer": 120}.get(key, 150)
                if x + width > WIDTH - LEFT:
                    x, top = picture_x, row_bottom + 10
                row_bottom = max(row_bottom, d.picture(canvas, x, top, image, width, caption=caption))
                x += width + 16
            y = max(bottom, row_bottom) + 18
        y += 12
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--survey", type=Path, required=True)
    parser.add_argument("--layouts", type=Path)
    parser.add_argument("--repo", type=Path, help="an Icechunk repository directory written by ingest.py")
    parser.add_argument("--archive", type=Path, metavar="SAMPLES",
                        help="draw archive.svg only, with pictures from this .npz cache of samples, "
                             "fetched by archive_samples.py when it does not exist")
    parser.add_argument("--rois", type=Path, help="with --archive: ROI counts per scan and field (JSON)")
    parser.add_argument("--cave", type=Path, help="with --archive: a directory of CAVE's match-table dumps")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    # The source buckets are public, and requests signed with other credentials would be refused.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    if args.archive:
        import archive_samples
        from diagrams_processed import read_cave

        from plumbing import dandi_assets

        survey = read_survey(args.survey)
        assets = dandi_assets("000402", "0.230307.2132")
        nwb_url = next(asset["url"] for path, asset in assets.items() if path.endswith(EXAMPLE_NWB))
        samples = archive_samples.cached(args.archive, nwb_url)
        cave = read_cave(args.cave)
        cave_rows = [(r.version, r.table, None if r.changed != r.changed or r.changed is None
                      else 100 * r.changed / r.rows) for r in cave.itertuples()]
        args.out.mkdir(parents=True, exist_ok=True)
        size = archive_figure(survey, json.loads(args.rois.read_text()), cave_rows, samples).write(
            args.out / "archive.svg")
        print(f"archive.svg  {size / 1e3:.0f} kB")
        return
    if not (args.layouts and args.repo):
        parser.error("--layouts and --repo are needed for the source, mapping and store figures")
    facts = Facts(read_survey(args.survey), read_layouts(args.layouts), read_store(args.repo))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, figure in (("source", source_figure), ("mapping", mapping_figure), ("store", store_figure)):
        size = figure(facts).write(args.out / f"{name}.svg")
        print(f"{name}.svg  {size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
