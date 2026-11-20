"""Draw the CAMELYON ingest: the public bucket, how its files map onto the store, and the store.

    uv run --package camelyon-ingest python ingests/camelyon/diagrams.py \\
        [--listing c17_listing.txt] [--census census17.jsonl] [--stages stages.csv] \\
        [--store17 <repo dir>] [--store16 <repo dir>]

Writes ``source.svg``, ``mapping.svg`` and ``store.svg`` into ``diagrams/`` beside this file.
The values in them are read from these inputs, none of which touches the bucket or a pixel:

- ``--listing``, an ``aws s3 ls --recursive`` of ``CAMELYON17/``, gives CAMELYON17's file
  counts and sizes and the patients and slides it holds;
- ``--census``, one JSON line per CAMELYON17 TIFF from the header census, gives the two TIFF
  dialects (generic and Philips), their pyramids and their extra pages;
- ``--stages``, the bucket's own ``stages.csv``, gives the labels and the training and test split;
- ``--store17`` and ``--store16``, local Icechunk repos opened read-only, give every shape,
  chunk shape, codec and attribute of the slides they hold: a trial of CAMELYON17 slides and
  a few CAMELYON16 slides. Their counts are samples, so each figure sets them against the
  archive's full counts, which come from the listing and the census.

The defaults point into the ignored ``.private/camelyon/`` directory. No listing of
CAMELYON16 is kept, so its counts are the constants in :data:`CAMELYON16_SOURCE`.

LAYOUTS.md explains the mapping; the figures carry no prose.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import zarr

from minus80 import diagrams as d
from minus80.core.icechunk_io import open_repository
from minus80.diagrams import ACCENTS, Canvas, CubeGeometry, Row, TreeNode

HERE = Path(__file__).parent
OUT = HERE / "diagrams"
PRIVATE = Path(".private/camelyon")

BUCKET = "s3://camelyon-dataset"

# CAMELYON16 has no listing in this repository. These are its counts and sizes from the survey
# table ("The source") in the CAMELYON ingest's LAYOUTS.md, taken from an ``aws s3 ls
# --recursive`` of the bucket on 2026-09-24: (objects, bytes). The same survey gives the
# numbers of slides with a Macro page and a Label page.
CAMELYON16_SOURCE = {
    "images": (399, 752.5e9),
    "masks": (399, 9.41e9),
    "background_tissue": (400, 0.47e9),
    "annotations": (160, 83e6),
    "evaluation": (3, 28e3),
    "root": (4, 67e3),
}
CAMELYON16_SLIDES = {"normal": 159, "tumor": 111, "test": 129}
CAMELYON16_MACRO_SLIDES = 246
CAMELYON16_LABEL_SLIDES = 2

#: The slide the mapping figure follows through CAMELYON17: it has a mask and outlines.
EXAMPLE_17 = "patient_004_node_4"
#: A CAMELYON17 slide with a Macro page, and one whose mask has a level the file lacks.
MACRO_17 = "patient_081_node_0"
DERIVED_17 = "patient_081_node_0"
#: The CAMELYON16 slide that has a tissue map and a Macro page.
EXAMPLE_16 = "tumor_056"


# Reading the inputs


@dataclass
class Listing:
    """CAMELYON17's ``aws s3 ls`` listing: each directory's file count and bytes, and the slides in it."""

    directories: dict[str, tuple[int, int]]
    sizes: dict[str, int]
    slides: list[str]
    patients: list[str]

    def objects(self) -> int:
        return sum(n for n, _ in self.directories.values())


def read_listing(path: Path) -> Listing:
    count, size, sizes = Counter(), Counter(), {}
    for line in path.read_text().splitlines():
        _, _, nbytes, key = line.split(maxsplit=3)
        if key.endswith("/"):
            continue
        parts = key.split("/")
        directory = parts[1] if len(parts) > 2 else "root"
        count[directory] += 1
        size[directory] += int(nbytes)
        sizes[key] = int(nbytes)
    slides = sorted(Path(k).stem for k in sizes if k.startswith("CAMELYON17/images/"))
    return Listing(
        {name: (count[name], size[name]) for name in count},
        sizes,
        slides,
        sorted({s.rsplit("_node_", 1)[0] for s in slides}),
    )


@dataclass
class Census:
    """The header census of CAMELYON17's TIFFs, one record per file."""

    records: list[dict]

    def images(self, philips: bool) -> list[dict]:
        return [r for r in self.records if "/images/" in r["key"] and r["philips"] == philips]

    def masks(self) -> list[dict]:
        return [r for r in self.records if "/masks/" in r["key"]]

    def record(self, directory: str, name: str) -> dict:
        return next(r for r in self.records if r["key"] == f"CAMELYON17/{directory}/{name}.tif")


def read_census(path: Path) -> Census:
    return Census([json.loads(line) for line in path.read_text().splitlines()])


@dataclass
class Stages:
    """``stages.csv``: a pN stage per training patient and a label per training slide, with the center."""

    patient_rows: list[dict]
    slide_rows: list[dict]

    @property
    def training(self) -> set[str]:
        return {r["patient"].removesuffix(".zip") for r in self.patient_rows}

    def row(self, name: str) -> dict:
        return next(r for r in self.patient_rows + self.slide_rows if r["patient"].rsplit(".", 1)[0] == name)


def read_stages(path: Path) -> Stages:
    rows = list(csv.DictReader(path.open()))
    return Stages(
        [r for r in rows if r["patient"].endswith(".zip")],
        [r for r in rows if r["patient"].endswith(".tif")],
    )


@dataclass
class Store:
    """A store's groups and arrays by path, and the snapshot they were read from."""

    snapshot: str
    root: zarr.Group
    groups: dict[str, zarr.Group]
    arrays: dict[str, zarr.Array]

    def count(self, pattern: str) -> int:
        return sum(bool(re.fullmatch(pattern, p)) for p in self.groups)

    def levels(self, prefix: str) -> list[zarr.Array]:
        """The level arrays directly under ``prefix``, finest first."""
        found = {int(m[1]): a for p, a in self.arrays.items() if (m := re.fullmatch(re.escape(prefix) + r"/(\d+)", p))}
        return [found[k] for k in sorted(found)]


def read_store(location: Path) -> Store:
    session = open_repository(location).readonly_session("main")
    root = zarr.open_group(session.store, mode="r")
    groups, arrays = {}, {}
    for path, node in root.members(max_depth=None):
        (arrays if isinstance(node, zarr.Array) else groups)[path] = node
    return Store(session.snapshot_id, root, groups, arrays)


def codec_text(array: zarr.Array) -> str:
    """The array's codec chain by name, as ``transpose, imagecodecs_jpeg``."""
    names = []
    for codec in array.metadata.codecs:
        info = codec.to_dict()
        names.append(info["name"] if isinstance(info, dict) else type(codec).__name__)
    return ", ".join(names)


def shape_range(arrays: list[zarr.Array]) -> str:
    """Shapes that differ from slide to slide, as ``3 × 197226 × 96651`` or ``3 × 76800–221184 × …``."""
    return d.dims([d.span(min(axis), max(axis)) for axis in zip(*(a.shape for a in arrays))])


def held(built: int, full: int) -> str:
    return f"{built:,} of {full:,}"


# Layout shared by the tree figures

WIDTH = 1400
TREE_X = 24
PANEL_X = 470
PANEL_WIDTH = 620
CUBE_X = 1150
GAP = 14


def tree_figure(canvas: Canvas, top: float, entries: list[tuple[TreeNode, dict]]) -> float:
    """A tree down the left with one panel per node on the right, each node level with its panel's title.

    Each entry is a node (its ``y`` is ignored) and the keyword arguments of its
    :func:`~minus80.diagrams.panel`. An entry may carry ``cube``, a function that draws
    beside the panel from the panel's top y.
    """
    y = top
    nodes = []
    for node, spec in entries:
        cube = spec.pop("cube", None)
        bottom = d.panel(canvas, PANEL_X, y, PANEL_WIDTH, **spec)
        if cube:
            bottom = max(bottom, cube(y))
        nodes.append(TreeNode(node.name, y + 15, node.depth, node.count, node.subtitle, node.status))
        y = bottom + GAP
    d.tree(canvas, TREE_X, nodes, leader_to=PANEL_X)
    return y


def section(canvas: Canvas, y: float, title: str, subtitle: str) -> float:
    """A rule across the figure with a section title above it."""
    canvas.add(f'<line x1="24" y1="{y:.1f}" x2="{canvas.width - 24}" y2="{y:.1f}" stroke="{d.RULE}"/>', y)
    canvas.text(24, y + 22, title, size=14, weight="bold")
    canvas.text(24 + d.text_width(title, 14, bold=True) + 12, y + 22, subtitle, size=12, color=d.MUTED)
    return y + 40


def native(key: str, value: str) -> Row:
    """A row for something written into the store, not referenced from the source."""
    return Row(key, value, status="added", tag="native")


# The source figure
#
# The bucket holds two challenges side by side. CAMELYON17's counts come from its listing and
# header census; CAMELYON16's from CAMELYON16_SOURCE.


def source_figure(listing: Listing, census: Census, stages: Stages, trial: Store, port: Store) -> Canvas:
    source16 = CAMELYON16_SOURCE
    objects16 = sum(n for n, _ in source16.values())
    generic, philips = census.images(False), census.images(True)
    masks = census.masks()
    training = stages.training
    test_patients = [p for p in listing.patients if p not in training]
    images17, images17_bytes = listing.directories["images"]
    license_ = trial.root.attrs["license"]

    def dialect_rows(records: list[dict]) -> list[Row]:
        levels = [len(r["levels"]) for r in records]
        slides = [r["key"].rsplit("/", 1)[1].removesuffix(".tif") for r in records]
        in_training = sum(s.rsplit("_node_", 1)[0] in training for s in slides)
        first = records[0]
        macro = sum(any(name == "Macro" for name, _ in r["series"]) for r in records)
        sizes = sum(r["size"] for r in records)
        return [
            Row("slides", f"{len(records):,}   {d.size_text(sizes)}"),
            Row("split", f"{in_training} in training patients, {len(records) - in_training} in test patients"),
            Row("pyramid", f"{d.span(min(levels), max(levels))} levels, {d.dims(first['tile'])} tiles"),
            Row("pixels", f"{first['compression']} {first['photometric']}, {first['dtype']}"),
            Row("extra pages", f"{macro} with a Macro page" if macro else "none"),
        ]

    def directory(name: str, source: str, rows: list[Row], count: int | None = None, depth: int = 2):
        return (
            TreeNode(f"{name}/", 0, depth, count=count),
            dict(title=f"{name}/", source=source, accent=ACCENTS["source"], rows=rows),
        )

    def sized(pair: tuple[int, float]) -> str:
        return f"{pair[0]:,} files   {d.size_text(int(pair[1]))}"

    levels_masks = [len(r["levels"]) for r in masks]
    annotations17 = listing.directories["annotations"]
    evaluation17 = listing.directories["evaluation"]
    root17 = listing.directories["root"]
    label_counts = Counter(r["stage"] for r in stages.slide_rows)
    stage_counts = Counter(r["stage"] for r in stages.patient_rows)
    centers = Counter(r["center"] for r in stages.patient_rows + stages.slide_rows)
    root_names = sorted(k.split("/")[-1] for k in listing.sizes if k.count("/") == 1)

    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Source", f"{BUCKET}  (us-west-2, anonymous)")
    y = d.legend(canvas, canvas.margin, y + 14, [(ACCENTS["source"], "source")])
    entries = [
        (
            TreeNode("camelyon-dataset", 0, 0, subtitle="bucket"),
            dict(
                title="s3://camelyon-dataset",
                source="two challenges side by side",
                accent=ACCENTS["source"],
                rows=[
                    Row("objects", f"{objects16 + listing.objects():,}"),
                    Row("license", license_),
                    Row("CAMELYON16", sized((objects16, sum(b for _, b in source16.values())))),
                    Row("CAMELYON17", sized((listing.objects(), sum(b for _, b in listing.directories.values())))),
                ],
            ),
        ),
        (
            TreeNode("CAMELYON16/", 0, 1, subtitle="detection of metastases"),
            dict(
                title="CAMELYON16/",
                source="no listing kept: counts from the survey in LAYOUTS.md",
                accent=ACCENTS["source"],
                rows=[Row("slides", f"{source16['images'][0]}   {', '.join(f'{n} {k}' for k, n in CAMELYON16_SLIDES.items())}")],
            ),
        ),
        directory(
            "images",
            "CAMELYON16/images/<slide>.tif",
            [Row("files", sized(source16["images"])), Row("dialects", "Philips BigTIFF ×398, plain BigTIFF ×1")],
        ),
        directory(
            "masks",
            "CAMELYON16/masks/<slide>_mask.tif",
            [Row("files", sized(source16["masks"])), Row("values", "0 background, 1 normal tissue, 2 tumor")],
        ),
        directory(
            "background_tissue",
            "CAMELYON16/background_tissue/<slide>_tissue.tif",
            [Row("files", sized(source16["background_tissue"])), Row("also", "results.json, the segmentation log")],
        ),
        directory(
            "annotations",
            "CAMELYON16/annotations/<slide>.xml",
            [Row("files", sized(source16["annotations"])), Row("format", "ASAP polygons")],
        ),
        directory(
            "evaluation",
            "CAMELYON16/evaluation/",
            [Row("files", sized(source16["evaluation"])), Row("holds", "reference.csv, evaluate.py, example.csv")],
        ),
        directory(
            "(root files)",
            "CAMELYON16/",
            [Row("files", sized(source16["root"])), Row("holds", "README.md, license.txt, checksums.md5, a model JSON")],
        ),
        (
            TreeNode("CAMELYON17/", 0, 1, subtitle="pN staging"),
            dict(
                title="CAMELYON17/",
                source="listing of CAMELYON17/",
                accent=ACCENTS["source"],
                rows=[
                    Row("slides", f"{len(listing.slides):,}   {len(listing.patients)} patients × 5 nodes"),
                    Row("labeled", f"{len(training)} training patients, {len(stages.slide_rows)} slides"),
                    Row("unlabeled", f"{len(test_patients)} test patients, {len(test_patients) * 5} slides"),
                ],
            ),
        ),
        directory(
            "images",
            "CAMELYON17/images/patient_NNN_node_N.tif",
            [Row("files", sized((images17, images17_bytes))), Row("dialects", f"generic TIFF ×{len(generic)}, Philips TIFF ×{len(philips)}")],
        ),
        (
            TreeNode("generic TIFF", 0, 3, count=len(generic), subtitle="ASAP-converted"),
            dict(
                title="generic TIFF",
                source="no ImageDescription, resolution in the TIFF tags",
                accent=ACCENTS["source"],
                rows=dialect_rows(generic),
            ),
        ),
        (
            TreeNode("Philips TIFF", 0, 3, count=len(philips), subtitle="Philips DP"),
            dict(
                title="Philips TIFF",
                source="Philips XML in ImageDescription, a Macro page",
                accent=ACCENTS["source"],
                rows=dialect_rows(philips),
            ),
        ),
        directory(
            "masks",
            "CAMELYON17/masks/patient_NNN_node_N_mask.tif",
            [
                Row("files", sized(listing.directories["masks"])),
                Row("pyramid", f"{d.span(min(levels_masks), max(levels_masks))} levels, {masks[0]['compression']}"),
            ],
        ),
        directory(
            "annotations",
            "CAMELYON17/annotations/patient_NNN_node_N.xml",
            [Row("files", sized(annotations17)), Row("format", "ASAP polygons")],
        ),
        directory(
            "evaluation",
            "CAMELYON17/evaluation/",
            [Row("files", sized(evaluation17)), Row("holds", "evaluate.py, example.csv")],
        ),
        directory(
            "(root files)",
            "CAMELYON17/",
            [Row("files", sized(root17)), Row("holds", ", ".join(root_names))],
        ),
        (
            TreeNode("stages.csv", 0, 2, subtitle="label table"),
            dict(
                title="stages.csv",
                source=f"CAMELYON17/stages.csv, {listing.sizes['CAMELYON17/stages.csv']:,} B",
                accent=ACCENTS["source"],
                rows=[
                    Row("rows", f"{len(stages.patient_rows) + len(stages.slide_rows)}   patient, stage, center"),
                    Row("patient rows", f"{len(stages.patient_rows)}   {', '.join(f'{k} ×{v}' for k, v in sorted(stage_counts.items()))}"),
                    Row("slide rows", f"{len(stages.slide_rows)}   {', '.join(f'{k} ×{label_counts[k]}' for k in ('negative', 'itc', 'micro', 'macro'))}"),
                    Row("centers", ", ".join(f"{k} ×{v}" for k, v in sorted(centers.items()))),
                    Row("test patients", f"{len(test_patients)}, none listed"),
                ],
            ),
        ),
    ]
    tree_figure(canvas, y + 20, entries)
    return canvas


# The mapping figure
#
# Each lane follows one source file onto the store nodes it becomes, for one real slide. A
# source array and its store array have the same shape, because a store array's chunks are the
# source's own tiles; the middle panel lists every level so the whole pyramid is visible.

LEFT = 24
RIGHT = 900
MIDDLE = 430
MIDDLE_WIDTH = 400


def cube_size(shape: tuple[int, int], *, tall: float = 190, wide: float = 190) -> tuple[float, float]:
    """A cube face for a (y, x) shape, at most ``tall`` high and ``wide`` across, in proportion."""
    aspect = shape[0] / shape[1]
    width = min(wide, tall / aspect)
    return width, width * aspect


def pyramid_lane(
    canvas: Canvas,
    y: float,
    *,
    source: dict,
    store: dict,
    levels: dict | None = None,
    extra: list[dict] = (),
) -> float:
    """One source array on the left, its store array on the right, and optionally every level between.

    ``source`` and ``store`` hold a cube's ``title``, ``lines``, ``shape`` (y, x) and
    ``origin`` line; ``levels`` and each ``extra`` are the keyword arguments of a panel.
    """
    cubes = []
    for spec, x, accent in ((source, LEFT, ACCENTS["source"]), (store, RIGHT, ACCENTS["store"])):
        width, height = cube_size(spec["shape"])
        geometry = CubeGeometry(x, y + 6, width, height, 28)
        bottom = d.cube(
            canvas,
            geometry,
            spec["title"],
            spec["lines"],
            accent=accent,
            source=spec["origin"],
            label_width=MIDDLE - LEFT - 20 if x == LEFT else WIDTH - RIGHT - 24,
        )
        cubes.append((geometry, bottom))
    (src, src_bottom), (dst, dst_bottom) = cubes
    bottom = max(src_bottom, dst_bottom)
    if levels is None:
        d.link(canvas, src.middle_right(), dst.middle_left(), label=store.get("link", "virtual"))
        return bottom + 36
    panel_bottom = d.panel(canvas, MIDDLE, y + 6, MIDDLE_WIDTH, accent=ACCENTS["store"], **levels)
    d.link(canvas, src.middle_right(), (MIDDLE, y + 40))
    d.link(canvas, (MIDDLE + MIDDLE_WIDTH, y + 40), dst.middle_left(), label=store.get("link", "virtual"))
    for spec in extra:
        panel_bottom = d.panel(canvas, MIDDLE, panel_bottom + GAP, MIDDLE_WIDTH, accent=ACCENTS["store"], **spec)
    return max(bottom, panel_bottom) + 36


def level_rows(arrays: list[zarr.Array], prefix: str) -> list[Row]:
    return [Row(f"{prefix}/{k}", d.dims(a.shape)) for k, a in enumerate(arrays)]


def tiff_lines(tags: dict, shape: tuple[int, ...], tile: tuple[int, ...]) -> list[str]:
    """A source array's lines for a cube: the page's shape, its tiles and how they are compressed."""
    return [
        f"shape  {d.dims(shape)}",
        f"tiles  {d.dims(tile)}",
        f"codec  {tags['Compression']}, {tags['PhotometricInterpretation']}",
    ]


def store_lines(array: zarr.Array) -> list[str]:
    return [f"shape  {d.dims(array.shape)}", f"chunks {d.dims(array.chunks)}", f"codec  {codec_text(array)}"]


def mapping_figure(listing: Listing, census: Census, stages: Stages, trial: Store, port: Store) -> Canvas:
    slide = EXAMPLE_17
    patient, node = slide.rsplit("_node_", 1)
    base = f"CAMELYON17/{patient}/node_{node}"
    image_levels = trial.levels(f"{base}/image")
    mask_levels = trial.levels(f"{base}/image/labels/mask")
    image_tags = trial.groups[f"{base}/image"].attrs["tiff_tags"][0]
    mask_tags = trial.groups[f"{base}/image/labels/mask"].attrs["tiff_tags"][0]
    image_census = census.record("images", slide)
    mask_census = census.record("masks", f"{slide}_mask")
    tile = tuple(image_census["tile"])
    shapes = trial.arrays[f"{base}/shapes/coords"].shape[0]
    shape_attrs = trial.groups[f"{base}/shapes"].attrs
    derived = f"CAMELYON17/{DERIVED_17.rsplit('_node_', 1)[0]}/node_{DERIVED_17.rsplit('_node_', 1)[1]}/image/labels/mask"
    derived_levels = trial.groups[derived].attrs["derived_levels"]
    derived_arrays = {k: trial.arrays[f"{derived}/{k}"] for k in derived_levels}
    macro_path = f"CAMELYON17/{MACRO_17.rsplit('_node_', 1)[0]}/node_{MACRO_17.rsplit('_node_', 1)[1]}/macro"
    macro = trial.levels(macro_path)[0]
    tissue_path = f"CAMELYON16/{EXAMPLE_16}/tissue"
    tissue_levels = port.levels(tissue_path)
    tissue_tags = port.groups[tissue_path].attrs["tiff_tags"][0]
    xml = f"CAMELYON17/annotations/{slide}.xml"
    patient_row = stages.row(patient)
    slide_row = stages.row(slide)
    root17 = trial.groups["CAMELYON17"].attrs
    slide_table = root17["annotations"]
    patient_table = root17["patients"]

    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Mapping", f"source {slide} → store {base}")
    y = d.legend(
        canvas,
        canvas.margin,
        y + 14,
        [(ACCENTS["source"], "source"), (ACCENTS["store"], "store, virtual chunks"), ("added", "native: written to the store"), ("arrow", "maps to")],
    )

    y = section(canvas, y + 20, "image", f"CAMELYON17/images/{slide}.tif → {patient}/node_{node}/image/0–{len(image_levels) - 1}")
    y = pyramid_lane(
        canvas,
        y,
        source=dict(
            title="images/….tif page 0",
            origin=f"{len(image_census['levels'])} pages, one per level",
            lines=tiff_lines(image_tags, (3, *image_census["levels"][0]), tile),
            shape=tuple(image_census["levels"][0]),
        ),
        store=dict(
            title="image/0",
            origin="each chunk is one source tile",
            lines=store_lines(image_levels[0]),
            shape=image_levels[0].shape[1:],
        ),
        levels=dict(
            title=f"image/0–{len(image_levels) - 1}",
            source="one array per TIFF page",
            rows=level_rows(image_levels, "image"),
        ),
    )

    y = section(canvas, y, "mask", f"CAMELYON17/masks/{slide}_mask.tif → …/image/labels/mask/0–{len(mask_levels) - 1}")
    y = pyramid_lane(
        canvas,
        y,
        source=dict(
            title="masks/….tif page 0",
            origin=f"{len(mask_census['levels'])} pages, one per level",
            lines=tiff_lines(mask_tags, tuple(mask_census["levels"][0]), tile),
            shape=tuple(mask_census["levels"][0]),
        ),
        store=dict(
            title="image/labels/mask/0",
            origin="an OME-Zarr label of the image",
            lines=store_lines(mask_levels[0]),
            shape=mask_levels[0].shape,
        ),
        levels=dict(
            title=f"mask/0–{len(mask_levels) - 1}",
            source="one array per TIFF page",
            rows=level_rows(mask_levels, "mask"),
        ),
        extra=[
            dict(
                title=f"mask/{k} of {DERIVED_17}",
                source=f"derived from level {int(k) - 1}, not in the file",
                rows=[native("shape", d.dims(a.shape))],
            )
            for k, a in derived_arrays.items()
        ],
    )

    y = section(canvas, y, "macro", f"images/{MACRO_17}.tif page Macro → …/macro/0 (Philips slides)")
    y = pyramid_lane(
        canvas,
        y,
        source=dict(
            title="images/….tif page Macro",
            origin="one JPEG strip",
            lines=[f"shape  {d.dims(macro.shape)}", f"strips 1, the whole page", "codec  JPEG"],
            shape=macro.shape[1:],
        ),
        store=dict(title="macro/0", origin="a one-level image", lines=store_lines(macro), shape=macro.shape[1:]),
    )

    y = section(canvas, y, "outlines", f"{xml} → {patient}/node_{node}/shapes")
    top = y
    left_bottom = d.panel(
        canvas,
        LEFT,
        top,
        360,
        f"{slide}.xml",
        [
            Row("size", f"{listing.sizes[xml]:,} B"),
            Row("shapes", str(trial.arrays[f"{base}/shapes/name"].shape[0])),
            Row("vertices", str(shapes)),
            Row("groups", ", ".join(sorted({g["name"] for g in shape_attrs["asap_groups"]}))),
        ],
        accent=ACCENTS["source"],
        source="ASAP polygons",
    )
    names = ("coords", "offset0", "name", "type", "group", "color", "geojson")
    rows = []
    for name in names:
        a = trial.arrays[f"{base}/shapes/{name}"]
        dtype = "string" if "String" in str(a.dtype) else str(a.dtype)
        rows.append(native(name, f"{d.dims(a.shape)}  {dtype}"))
    right_bottom = d.panel(canvas, 520, top, 560, "shapes/", rows, accent=ACCENTS["store"], source="vertex table, one row per shape; geojson for QuPath")
    d.link(canvas, (LEFT + 360, top + 50), (520, top + 50), label="native")
    y = max(left_bottom, right_bottom) + 36

    y = section(canvas, y, "labels", "CAMELYON17/stages.csv → attributes")
    top = y
    stage_rows = [
        Row(patient_row["patient"], f"{patient_row['stage']}, center {patient_row['center']}"),
        Row(slide_row["patient"], f"{slide_row['stage']}, center {slide_row['center']}"),
        Row("columns", "patient, stage, center"),
    ]
    left_bottom = d.panel(canvas, LEFT, top, 360, "stages.csv", stage_rows, accent=ACCENTS["source"], source=f"{len(stages.patient_rows)} patient rows, {len(stages.slide_rows)} slide rows")
    patient_attrs = trial.groups[f"CAMELYON17/{patient}"].attrs["annotations"]
    slide_attrs = trial.groups[base].attrs["annotations"]
    targets = [
        (
            "CAMELYON17/  attributes",
            [
                native("annotations", f"slide table, {len(slide_table)} columns × {len(slide_table['slide'])} rows"),
                native("patients", f"patient table, {len(patient_table)} columns × {len(patient_table['group'])} rows"),
                native("center_names", f"{len(root17['center_names'])} centers"),
            ],
        ),
        (
            f"{patient}/  attributes",
            [native("annotations", f"stage {patient_attrs['stage']}, center {patient_attrs['center']}, {len(patient_attrs['slides'])} slides")],
        ),
        (
            f"{patient}/node_{node}/  attributes",
            [native("annotations", f"stage {slide_attrs['stage']}, center {slide_attrs['center']}, {slide_attrs['n_shapes']} shapes")],
        ),
    ]
    ty = top
    right_bottom = top
    for i, (title, trows) in enumerate(targets):
        right_bottom = d.panel(canvas, 520, ty, 700, title, trows, accent=ACCENTS["store"])
        d.link(canvas, (LEFT + 360, top + 50), (520, ty + 28), color=d.MUTED)
        ty = right_bottom + GAP
    y = max(left_bottom, right_bottom) + 36

    y = section(canvas, y, "tissue map (CAMELYON16 only)", f"CAMELYON16/background_tissue/{EXAMPLE_16}_tissue.tif → {EXAMPLE_16}/tissue/0–{len(tissue_levels) - 1}")
    y = pyramid_lane(
        canvas,
        y,
        source=dict(
            title="background_tissue/….tif page 0",
            origin=f"{len(tissue_levels)} pages",
            lines=tiff_lines(tissue_tags, tissue_levels[0].shape, tuple(tissue_levels[0].chunks)),
            shape=tissue_levels[0].shape,
        ),
        store=dict(
            title="tissue/0",
            origin="an image, not a label of the slide",
            lines=store_lines(tissue_levels[0]),
            shape=tissue_levels[0].shape,
        ),
        levels=dict(
            title=f"tissue/0–{len(tissue_levels) - 1}",
            source="one array per TIFF page",
            rows=level_rows(tissue_levels, "tissue"),
        ),
    )
    canvas.bottom = max(canvas.bottom, y - 20)
    return canvas


# The store figure
#
# Panels describe the built stores. A node's ×N is how many such nodes the full archive has,
# from the listing, the census and the CAMELYON16 survey; each panel's first row says how many
# the built store holds.


def store_figure(listing: Listing, census: Census, stages: Stages, trial: Store, port: Store) -> Canvas:
    slides17 = len(listing.slides)
    patients17 = len(listing.patients)
    masks17 = listing.directories["masks"][0]
    shapes17 = listing.directories["annotations"][0]
    macro17 = len(census.images(True))
    slides16 = CAMELYON16_SOURCE["images"][0]
    shapes16 = CAMELYON16_SOURCE["annotations"][0]
    tissue16 = CAMELYON16_SOURCE["background_tissue"][0] - 1  # every file but results.json
    root_attrs = trial.root.attrs
    attrs16 = port.groups["CAMELYON16"].attrs
    attrs17 = trial.groups["CAMELYON17"].attrs
    ex16 = f"CAMELYON16/{EXAMPLE_16}"
    ex17 = f"CAMELYON17/{EXAMPLE_17.rsplit('_node_', 1)[0]}/node_{EXAMPLE_17.rsplit('_node_', 1)[1]}"
    row16 = port.groups[ex16].attrs["annotations"]
    row17 = trial.groups[ex17].attrs["annotations"]

    built16 = port.count(r"CAMELYON16/(?!OME$|documents$)[^/]+")
    built17_patients = trial.count(r"CAMELYON17/patient_\d+")
    built17 = trial.count(r"CAMELYON17/patient_\d+/node_\d")
    images16 = port.count(r"CAMELYON16/[^/]+/image")
    images17 = trial.count(r"CAMELYON17/patient_\d+/node_\d/image")
    masks_built16 = port.count(r"CAMELYON16/[^/]+/image/labels/mask")
    masks_built17 = trial.count(r"CAMELYON17/patient_\d+/node_\d/image/labels/mask")
    derived_masks17 = sum(
        "derived_levels" in g.attrs for p, g in trial.groups.items() if re.fullmatch(r"CAMELYON17/patient_\d+/node_\d/image/labels/mask", p)
    )
    tissue_built = port.count(r"CAMELYON16/[^/]+/tissue")
    macro_built16 = port.count(r"CAMELYON16/[^/]+/macro")
    macro_built17 = trial.count(r"CAMELYON17/patient_\d+/node_\d/macro")
    shapes_built16 = port.count(r"CAMELYON16/[^/]+/shapes")
    shapes_built17 = trial.count(r"CAMELYON17/patient_\d+/node_\d/shapes")

    image16 = port.groups[f"{ex16}/image"].attrs
    image17 = trial.groups[f"{ex17}/image"].attrs
    axes = [a["name"] for a in image17["ome"]["multiscales"][0]["coordinateSystems"][0]["axes"]]
    axes16 = [a["name"] for a in image16["ome"]["multiscales"][0]["axes"]]
    mask16 = port.groups[f"{ex16}/image/labels/mask"].attrs
    mask17 = trial.groups[f"{ex17}/image/labels/mask"].attrs
    tissue = port.groups[f"{ex16}/tissue"].attrs
    shapes_example = [f"{n} {d.dims(port.arrays[f'{ex16}/shapes/{n}'].shape)}" for n in ("coords", "offset0")]
    shapes_names17 = sorted(p.rsplit("/", 1)[1] for p in trial.arrays if p.startswith(f"{ex17}/shapes/"))
    documents16 = sorted(p.rsplit("/", 1)[1] for p in port.arrays if p.startswith("CAMELYON16/documents/"))
    slide_documents = sorted(p.rsplit("/", 1)[1] for p in port.arrays if p.startswith(f"{ex16}/documents/"))
    documents17 = sorted(p.rsplit("/", 1)[1] for p in trial.arrays if p.startswith("CAMELYON17/documents/"))
    series16 = port.groups["CAMELYON16/OME"].attrs["ome"]["series"]
    series17 = trial.groups["CAMELYON17/OME"].attrs["ome"]["series"]
    centers = attrs17["center_names"]

    canvas = Canvas(WIDTH)
    y = d.heading(
        canvas,
        "Store",
        f"panels: {built17} CAMELYON17 and {built16} CAMELYON16 slides built; ×N: the full archive",
    )
    y = d.legend(canvas, canvas.margin, y + 14, [(ACCENTS["store"], "store"), ("added", "native")])
    entries = [
        (
            TreeNode("/", 0, 0, subtitle="archive root"),
            dict(
                title="/  zarr.json",
                rows=[
                    Row("title", root_attrs["title"]),
                    Row("license", root_attrs["license"]),
                    Row("citation", ", ".join(root_attrs["citation"])),
                    Row("source", f"{root_attrs['source']['bucket']}  {', '.join(root_attrs['source']['prefixes'])}"),
                    Row("requires", ", ".join(root_attrs["requires"])),
                ],
            ),
        ),
        (
            TreeNode("CAMELYON16/", 0, 1, subtitle="bioformats2raw container"),
            dict(
                title="CAMELYON16/",
                rows=[
                    Row("built", f"{built16} of {slides16} slides"),
                    Row("ome", f"version {attrs16['ome']['version']}, bioformats2raw.layout {attrs16['ome']['bioformats2raw.layout']}"),
                    native("annotations", f"slide table, {len(attrs16['annotations'])} columns × {len(attrs16['annotations']['slide'])} rows"),
                    native("source_files", f"{len(attrs16['source_files'])} challenge files verbatim"),
                ],
            ),
        ),
        (
            TreeNode("OME/", 0, 2, subtitle="series list"),
            dict(title="CAMELYON16/OME", rows=[Row("series", f"{len(series16)} entries: images, macro, tissue")]),
        ),
        (
            TreeNode("documents/", 0, 2, subtitle="files over 64 KB"),
            dict(title="CAMELYON16/documents", rows=[native("arrays", ", ".join(documents16))]),
        ),
        (
            TreeNode("<slide>/", 0, 2, count=slides16, subtitle="one glass slide"),
            dict(
                title=f"CAMELYON16/<slide>   e.g. {EXAMPLE_16}",
                rows=[
                    Row("built", held(built16, slides16)),
                    native("annotations", f"class {row16['class']}, center {row16['center']}, {row16['scanner']}, {row16['n_shapes']} shapes"),
                ],
            ),
        ),
        (
            TreeNode("image/", 0, 3, count=slides16, subtitle="OME-Zarr image"),
            dict(
                title="<slide>/image",
                source="images/<slide>.tif",
                rows=[
                    Row("built", held(images16, slides16)),
                    Row("ome", f"version {image16['ome']['version']}, axes {', '.join(axes16)}"),
                    Row("pixel_size", f"{image16['pixel_size']['um'][0]:.4f} µm"),
                    Row("tiff_tags", f"{len(image16['tiff_tags'])} pages, source_url, source_md5"),
                ],
            ),
        ),
        (
            TreeNode("0 … N", 0, 4, subtitle="levels, 8–10 per slide"),
            dict(
                title="<slide>/image/<level>",
                source="one array per TIFF page",
                rows=[
                    Row("level 0", shape_range([a for p, a in port.arrays.items() if re.fullmatch(r"CAMELYON16/[^/]+/image/0", p)])),
                    Row("chunks", d.dims(port.arrays[f"{ex16}/image/0"].chunks)),
                    Row("dtype", str(port.arrays[f"{ex16}/image/0"].dtype)),
                    Row("codec", codec_text(port.arrays[f"{ex16}/image/0"])),
                ],
            ),
        ),
        (
            TreeNode("labels/mask/", 0, 4, count=slides16, subtitle="reference mask"),
            dict(
                title="<slide>/image/labels/mask",
                source="masks/<slide>_mask.tif",
                rows=[
                    Row("built", held(masks_built16, slides16)),
                    Row("levels", f"{len(port.levels(f'{ex16}/image/labels/mask'))}, as many as the image"),
                    native("level 9", "derived from level 8, not in the file") if mask16.get("derived_levels") else Row("derived", "none"),
                ],
            ),
        ),
        (
            TreeNode("tissue/", 0, 3, count=tissue16, subtitle="tissue segmentation"),
            dict(
                title="<slide>/tissue",
                source="background_tissue/<slide>_tissue.tif",
                rows=[
                    Row("built", held(tissue_built, tissue16)),
                    Row("levels", f"{len(port.levels(f'{ex16}/tissue'))}, level 0 {d.dims(port.levels(f'{ex16}/tissue')[0].shape)}"),
                    Row("matches", f"image level {tissue['matches_image_level']}"),
                ],
            ),
        ),
        (
            TreeNode("macro/", 0, 3, count=CAMELYON16_MACRO_SLIDES, subtitle="Macro page"),
            dict(
                title="<slide>/macro",
                source="images/<slide>.tif, page Macro",
                rows=[
                    Row("built", held(macro_built16, CAMELYON16_MACRO_SLIDES)),
                    Row("macro/0", d.dims(port.levels(f"{ex16}/macro")[0].shape)),
                    Row("label/", f"{CAMELYON16_LABEL_SLIDES} slides also have a Label page"),
                ],
            ),
        ),
        (
            TreeNode("shapes/", 0, 3, count=shapes16, subtitle="outlines"),
            dict(
                title="<slide>/shapes",
                source="annotations/<slide>.xml",
                rows=[
                    Row("built", held(shapes_built16, shapes16)),
                    native("arrays", ", ".join(sorted(p.rsplit("/", 1)[1] for p in port.arrays if p.startswith(f"{ex16}/shapes/")))),
                    native("shapes", ", ".join(shapes_example)),
                ],
            ),
        ),
        (
            TreeNode("documents/", 0, 3, subtitle="over 64 KB"),
            dict(title="<slide>/documents", rows=[native("arrays", ", ".join(slide_documents))]),
        ),
        (
            TreeNode("CAMELYON17/", 0, 1, subtitle="OME-Zarr 0.6 container"),
            dict(
                title="CAMELYON17/",
                rows=[
                    Row("built", f"{built17} of {slides17:,} slides, {built17_patients} of {patients17} patients"),
                    Row("ome", f"version {attrs17['ome']['version']}, bioformats2raw.layout {attrs17['ome']['bioformats2raw.layout']}"),
                    native("annotations", f"slide table, {len(attrs17['annotations'])} columns × {len(attrs17['annotations']['slide'])} rows"),
                    native("patients", f"patient table, {len(attrs17['patients'])} columns × {len(attrs17['patients']['group'])} rows"),
                    native("center_names", ", ".join(f"{k} {v.split(' (')[-1].rstrip(')')}" for k, v in centers.items())),
                ],
            ),
        ),
        (
            TreeNode("OME/", 0, 2, subtitle="series list"),
            dict(title="CAMELYON17/OME", rows=[Row("series", f"{len(series17)} entries: images, macro")]),
        ),
        (
            TreeNode("documents/", 0, 2, subtitle="files over 64 KB"),
            dict(title="CAMELYON17/documents", rows=[native("arrays", ", ".join(documents17))]),
        ),
        (
            TreeNode("<patient>/", 0, 2, count=patients17, subtitle="one patient"),
            dict(
                title=f"CAMELYON17/<patient>   e.g. {EXAMPLE_17.rsplit('_node_', 1)[0]}",
                source="a row of stages.csv",
                rows=[
                    Row("built", held(built17_patients, patients17)),
                    native("annotations", f"stage {trial.groups[ex17.rsplit('/', 1)[0]].attrs['annotations']['stage']}, 5 slides, split, center"),
                ],
            ),
        ),
        (
            TreeNode("<node>/", 0, 3, count=slides17, subtitle="one lymph node slide"),
            dict(
                title=f"<patient>/<node>   e.g. {ex17.rsplit('/', 1)[1]}",
                source="a row of stages.csv",
                rows=[
                    Row("built", held(built17, slides17)),
                    native("annotations", f"stage {row17['stage']}, center {row17['center']}, has_mask {row17['has_mask']}, n_shapes {row17['n_shapes']}"),
                ],
            ),
        ),
        (
            TreeNode("image/", 0, 4, count=slides17, subtitle="OME-Zarr image"),
            dict(
                title="<node>/image",
                source="images/<slide>.tif",
                rows=[
                    Row("built", held(images17, slides17)),
                    Row("ome", f"version {image17['ome']['version']}, axes {', '.join(axes)}"),
                    Row("pixel_size", f"{image17['pixel_size']['um'][0]:.4f} µm"),
                ],
            ),
        ),
        (
            TreeNode("0 … N", 0, 5, subtitle="levels, 8–10 per slide"),
            dict(
                title="<node>/image/<level>",
                source="one array per TIFF page",
                rows=[
                    Row("level 0", shape_range([a for p, a in trial.arrays.items() if re.fullmatch(r"CAMELYON17/[^/]+/[^/]+/image/0", p)])),
                    Row("chunks", d.dims(trial.arrays[f"{ex17}/image/0"].chunks)),
                    Row("dtype", str(trial.arrays[f"{ex17}/image/0"].dtype)),
                    Row("codec", codec_text(trial.arrays[f"{ex17}/image/0"])),
                ],
            ),
        ),
        (
            TreeNode("labels/mask/", 0, 5, count=masks17, subtitle="reference mask"),
            dict(
                title="<node>/image/labels/mask",
                source="masks/<slide>_mask.tif",
                rows=[
                    Row("built", held(masks_built17, masks17)),
                    Row("levels", f"{len(trial.levels(f'{ex17}/image/labels/mask'))}, as many as the file has"),
                    native("derived levels", f"{derived_masks17} of {masks_built17} built masks have a level the file lacks"),
                ],
            ),
        ),
        (
            TreeNode("macro/", 0, 4, count=macro17, subtitle="Macro page, Philips slides"),
            dict(
                title="<node>/macro",
                source="images/<slide>.tif, page Macro",
                rows=[
                    Row("built", held(macro_built17, macro17)),
                    Row("macro/0", d.dims(trial.levels(f"CAMELYON17/{MACRO_17.rsplit('_node_', 1)[0]}/node_{MACRO_17.rsplit('_node_', 1)[1]}/macro")[0].shape)),
                ],
            ),
        ),
        (
            TreeNode("shapes/", 0, 4, count=shapes17, subtitle="outlines"),
            dict(
                title="<node>/shapes",
                source="annotations/<slide>.xml",
                rows=[
                    Row("built", held(shapes_built17, shapes17)),
                    native("arrays", ", ".join(shapes_names17)),
                ],
            ),
        ),
    ]
    tree_figure(canvas, y + 20, entries)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listing", type=Path, default=PRIVATE / "c17_listing.txt")
    parser.add_argument("--census", type=Path, default=PRIVATE / "census17.jsonl")
    parser.add_argument("--stages", type=Path, default=PRIVATE / "src17/stages.csv")
    parser.add_argument("--store17", type=Path, default=PRIVATE / "repos/trial17")
    parser.add_argument("--store16", type=Path, default=PRIVATE / "repos/port16")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    inputs = (
        read_listing(args.listing),
        read_census(args.census),
        read_stages(args.stages),
        read_store(args.store17),
        read_store(args.store16),
    )
    args.out.mkdir(parents=True, exist_ok=True)
    for name, figure in (("source", source_figure), ("mapping", mapping_figure), ("store", store_figure)):
        size = figure(*inputs).write(args.out / f"{name}.svg")
        print(f"{name}.svg  {size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
