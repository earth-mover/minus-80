"""Draw the zebrafish ingest: the source zip, how it maps onto the store, and the store.

    ARRAYLAKE_TOKEN=... uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/diagrams.py \\
        --directory-cache <central-directory.parquet>

Writes ``source.svg``, ``mapping.svg``, ``crops.svg`` and ``store.svg`` into ``diagrams/``
beside this file. The values in them are read from two inputs:

- the zip's central directory, from the Parquet copy the converter keeps
  (``ingest.DIRECTORY_CACHE``), gives the zip's size, its members, and how
  many chunks each source array stores;
- the store on Arraylake, opened read-only, gives every attribute, shape,
  chunk shape, shard shape and codec, and the ROI rows the copy kept.

Nothing is fetched from EBI and no pixel is read. The source's array shapes
are not in the directory, only in each array's ``.zarray`` inside the zip, so
they are computed from what the store kept: a well image's extent is its
``well_ROI_table`` row divided by the 0.65 µm pixel, and each coarser level
halves it, rounding down, as the source's levels do. The few facts that
neither input holds (the source's OME-Zarr version, its codec and chunk
shapes) are constants below.

The mapping and crops figures cut every field's windows again with
:func:`minus80.ome.crop.crop_levels` and stop if they differ from the
shapes and translations in the store, so the drawn windows cannot drift
from the data.

LAYOUTS.md explains the mapping; the figures carry no prose.
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import ingest
import pyarrow.parquet as pq
import zarr
from zarr.codecs import BloscCodec
from pipeline import open_repository

from minus80 import diagrams as d
from minus80.diagrams import ACCENTS, Canvas, CubeGeometry, Row, TreeNode
from minus80.ome.crop import crop_levels

REPO = "arraylake://earthmover-biology/zebrafish-3d-4i"
OUT = Path(__file__).parent / "diagrams"

# Facts about the source that neither the directory nor the store records, from PLAN.md's
# survey of the zip. On 2026-11-11 the .zarray of F/04/0 levels 0 and 3 and of F/04/1's
# cells and nucleiRaw labels confirmed the chunk shapes, the codec, and the size rule
# above: (14786, 17192) at level 0 from the well_ROI_table, (1848, 2149) at level 3.
SOURCE_VERSION = "0.4"
SOURCE_CHUNKS = (1, 10, 1000, 1000)
SOURCE_LABEL_CHUNKS = (10, 1000, 1000)
SOURCE_CODEC = "blosc lz4 5, shuffle"

#: The well and field the mapping figure follows. FOV_2 of C/02 starts off the coarse grid on
#: both axes, so its coarse windows are wider than 1000 / 2^k.
EXAMPLE_WELL = "C/02"
EXAMPLE_IMAGE = "1"


# Reading the zip's central directory


IMAGE_CHUNK = re.compile(r"^([A-Z])/(\d\d)/(\d+)/(\d+)/(\d+)/(\d+)/(\d+)/(\d+)$")
LABEL_CHUNK = re.compile(r"^([A-Z])/(\d\d)/(\d+)/labels/([^/]+)/(\d+)/(\d+)/(\d+)/(\d+)$")
TABLE = re.compile(r"^([A-Z])/(\d\d)/(\d+)/tables/([^/]+)/")


@dataclass
class Listing:
    """What the zip's central directory says, counted the way the figures need it."""

    url: str
    archive_size: int
    directory_size: int
    members: int
    methods: Counter
    #: Per well: compressed bytes and member count.
    well_bytes: dict[str, int] = field(default_factory=dict)
    well_members: dict[str, int] = field(default_factory=dict)
    rounds: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    channels: set[int] = field(default_factory=set)
    #: Stored chunk members per image level, and how many of them are empty files.
    image_chunks: Counter = field(default_factory=Counter)
    image_empty: Counter = field(default_factory=Counter)
    label_chunks: Counter = field(default_factory=Counter)
    label_empty: Counter = field(default_factory=Counter)
    #: Label names, and the rounds that hold labels.
    labels: set[str] = field(default_factory=set)
    label_rounds: set[int] = field(default_factory=set)
    #: Each table's name and the rounds that hold it.
    tables: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    table_members: int = 0


def read_listing(path: Path) -> Listing:
    directory = pq.read_table(path, columns=["name", "compressed", "uncompressed", "method"])
    meta = {k.decode(): v.decode() for k, v in directory.schema.metadata.items()}
    names = directory["name"].to_pylist()
    compressed = directory["compressed"].to_pylist()
    uncompressed = directory["uncompressed"].to_pylist()
    listing = Listing(
        url=meta["url"],
        archive_size=int(meta["archive_size"]),
        directory_size=int(meta["directory_size"]),
        members=len(names),
        methods=Counter(directory["method"].to_pylist()),
    )
    well_bytes, well_members = Counter(), Counter()
    for name, size, raw in zip(names, compressed, uncompressed):
        parts = name.split("/")
        if len(parts) > 2 and parts[1].isdigit():
            well = f"{parts[0]}/{parts[1]}"
            well_bytes[well] += size
            well_members[well] += 1
        if match := IMAGE_CHUNK.match(name):
            level = int(match[4])
            listing.rounds[f"{match[1]}/{match[2]}"].add(int(match[3]))
            listing.channels.add(int(match[5]))
            listing.image_chunks[level] += 1
            listing.image_empty[level] += raw == 0
        elif match := LABEL_CHUNK.match(name):
            level = int(match[5])
            listing.labels.add(match[4])
            listing.label_rounds.add(int(match[3]))
            listing.label_chunks[level] += 1
            listing.label_empty[level] += raw == 0
        elif match := TABLE.match(name):
            listing.tables[match[4]].add(int(match[3]))
            listing.table_members += 1
    listing.well_bytes = dict(well_bytes)
    listing.well_members = dict(well_members)
    return listing


# Reading the store


@dataclass
class Store:
    """The store's groups and arrays, by path, with the snapshot they were read from."""

    snapshot: str
    root: zarr.Group
    groups: dict[str, zarr.Group]
    arrays: dict[str, zarr.Array]

    def wells(self) -> list[str]:
        return [w["path"] for w in self.root.attrs["ome"]["plate"]["wells"]]

    def images(self, well: str) -> list[str]:
        return [i["path"] for i in self.groups[well].attrs["ome"]["well"]["images"]]

    def image_paths(self) -> list[str]:
        return [f"{w}/{i}" for w in self.wells() for i in self.images(w)]


def read_store(location: str, branch: str) -> Store:
    session = open_repository(location).readonly_session(branch)
    root = zarr.open_group(session.store, mode="r")
    groups, arrays = {}, {}
    for path, node in root.members(max_depth=None):
        (arrays if isinstance(node, zarr.Array) else groups)[path] = node
    return Store(session.snapshot_id, root, groups, arrays)


def well_extent(store: Store, well: str) -> tuple[int, int]:
    """A source well image's level-0 (y, x) size, from the ``well_ROI_table`` row the store kept.

    The pixel size is the store's own level-0 scale, which the copy took from
    the source's multiscales.
    """
    row = store.groups[well].attrs["annotations"]["well_ROI_table"]["rows"]["Well"]
    first = store.images(well)[0]
    scale = image_scales(store, f"{well}/{first}")[0]
    return round(row["len_y_micrometer"] / scale[-2]), round(row["len_x_micrometer"] / scale[-1])


def image_scales(store: Store, image: str) -> list[list[float]]:
    datasets = store.groups[image].attrs["ome"]["multiscales"][0]["datasets"]
    return [ds["coordinateTransformations"][0]["transformations"][0]["scale"] for ds in datasets]


def image_translations(store: Store, image: str) -> list[list[float]]:
    datasets = store.groups[image].attrs["ome"]["multiscales"][0]["datasets"]
    return [ds["coordinateTransformations"][0]["transformations"][1]["translation"] for ds in datasets]


def source_levels(extent: tuple[int, int], levels: int = 4) -> list[tuple[int, int]]:
    """Each source level's (y, x), halving and rounding down from level 0."""
    return [(extent[0] >> k, extent[1] >> k) for k in range(levels)]


def codec_text(array: zarr.Array) -> str:
    """The array's inner codec, as ``blosc zstd 5, bitshuffle``."""
    for codec in array.metadata.codecs:
        for inner in getattr(codec, "codecs", (codec,)):
            if isinstance(inner, BloscCodec):
                return f"blosc {inner.cname} {inner.clevel}, {inner.shuffle}"
    return "none"


def axis_span(values) -> str:
    values = list(values)
    return d.span(min(values), max(values))


def shape_span(shapes: list[tuple[int, ...]]) -> str:
    """Shapes that differ on some axes, as ``16 × 271 × 500–501 × 500–501``."""
    return d.dims([axis_span(axis) for axis in zip(*shapes)])


def plane_text(leading: tuple[int, ...]) -> str:
    """The leading axes of a chunk or shard whose last two axes are the level's whole plane."""
    return d.dims(leading)


def micrometers(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") + " µm"


# Layout shared by the tree figures


WIDTH = 1400
TREE_X = 24
PANEL_X = 470
PANEL_WIDTH = 620
CUBE_X = 1150
GAP = 16


def tree_figure(canvas: Canvas, top: float, entries: list[tuple[TreeNode, dict]]) -> float:
    """A tree down the left with one panel per node on the right, each node level with its panel's title.

    Each entry is a node (its ``y`` is ignored) and the keyword arguments of
    its :func:`~minus80.diagrams.panel`. An entry may carry ``cube``, a
    function that draws beside the panel from the panel's top y.
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


def side_cube(
    canvas: Canvas,
    top: float,
    title: str,
    lines: list[str],
    aspect: tuple[float, float],
    *,
    width: float = 150,
    depth: float = 26,
    **style,
) -> float:
    """A cube for one array, in the column right of the panels, its face ``aspect`` (y, x) in proportion."""
    height = width * aspect[0] / aspect[1]
    return d.cube(
        canvas, CubeGeometry(CUBE_X, top + 8, width, height, depth), title, lines, label_width=WIDTH - CUBE_X - 24, **style
    )


# The source figure


def source_figure(listing: Listing, store: Store) -> Canvas:
    wells = store.wells()
    extents = {w: well_extent(store, w) for w in wells}
    levels = {w: source_levels(e) for w, e in extents.items()}
    fields_per_well = [len(store.images(w)) for w in wells]
    rounds = sorted(set().union(*listing.rounds.values()))
    plate = store.root.attrs["ome"]["plate"]
    source = store.root.attrs["source"]
    url = listing.url.removeprefix("https://")
    host, _, path = url.partition("/")
    folder, _, zip_name = path.rpartition("/")
    methods = ", ".join(f"{ {0: 'stored', 8: 'Deflate'}.get(m, m)} ×{n:,}" for m, n in sorted(listing.methods.items()))
    empty_label = sum(listing.label_empty.values())
    example = extents[EXAMPLE_WELL]
    pixel = image_scales(store, f"{EXAMPLE_WELL}/{EXAMPLE_IMAGE}")[0][-1]
    fields = [
        window(store.groups[f"{EXAMPLE_WELL}/{i}"].attrs["annotations"]["FOV_ROI_table"], pixel, example)
        for i in store.images(EXAMPLE_WELL)
    ]

    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Source", zip_name)
    y = d.legend(canvas, canvas.margin, y + 14, [(ACCENTS["source"], "source"), ("changed", "unlisted"), ("removed", "empty")])

    def level_rows() -> list[Row]:
        rows = []
        for k in range(4):
            ys = [levels[w][k][0] for w in wells]
            xs = [levels[w][k][1] for w in wells]
            shape = d.dims([len(listing.channels), 271, axis_span(ys), axis_span(xs)])
            rows.append(Row(f"level {k}", f"{shape}   {d.count_text(listing.image_chunks[k])} chunks"))
        rows.append(Row("chunks", d.dims(SOURCE_CHUNKS)))
        rows.append(
            Row("empty files", f"{sum(listing.image_empty.values()):,} chunks", status="removed", tag="empty")
        )
        return rows

    label_shape = d.dims([271, axis_span(e[0] for e in extents.values()), axis_span(e[1] for e in extents.values())])
    entries = [
        (
            TreeNode(zip_name, 0, 0, subtitle="zip"),
            dict(
                title=zip_name,
                source=f"EBI, BioImage Archive {source['study'].rsplit('/', 1)[-1]}",
                accent=ACCENTS["source"],
                rows=[
                    Row("host", host),
                    Row("path", f"/{folder}/"),
                    Row("size", f"{d.size_text(listing.archive_size)}  ({listing.archive_size:,} B)"),
                    Row("members", d.count_text(listing.members)),
                    Row("directory", d.size_text(listing.directory_size)),
                    Row("compression", methods),
                ],
            ),
        ),
        (
            TreeNode("/", 0, 1, subtitle="plate"),
            dict(
                title="/  .zattrs",
                source="OME-Zarr HCS plate, Zarr v2, Fractal",
                accent=ACCENTS["source"],
                rows=[
                    Row("version", SOURCE_VERSION),
                    Row("name", plate["name"]),
                    Row("rows", f"{len(plate['rows'])}  {plate['rows'][0]['name']}–{plate['rows'][-1]['name']}"),
                    Row("columns", f"{len(plate['columns'])}  {plate['columns'][0]['name']}–{plate['columns'][-1]['name']}"),
                    Row("wells", str(len(listing.well_bytes))),
                    Row(
                        "acquisitions",
                        ", ".join(str(a) for a in source["source_acquisitions"]),
                        status="changed",
                        tag="unlisted",
                    ),
                ],
            ),
        ),
        (
            TreeNode("<row>/<col>", 0, 2, count=len(listing.well_bytes), subtitle="well"),
            dict(
                title="<row>/<col>/.zattrs",
                source="HCS well",
                accent=ACCENTS["source"],
                rows=[
                    Row("images", ", ".join(str(a) for a in source["source_acquisitions"]), status="changed", tag="unlisted"),
                    Row("rounds held", d.span(rounds[0], rounds[-1])),
                    Row("fields", f"{axis_span(fields_per_well)} per well, {sum(fields_per_well)} in all"),
                    Row("bytes", f"{d.size_text(min(listing.well_bytes.values()))}–{d.size_text(max(listing.well_bytes.values()))} per well"),
                    Row("members", f"{min(listing.well_members.values()):,}–{max(listing.well_members.values()):,} per well"),
                ],
            ),
        ),
        (
            TreeNode("<round>", 0, 3, count=len(rounds), subtitle="well image"),
            dict(
                title="<row>/<col>/<round>",
                source="one well-sized image per round, fields pasted at stage positions",
                accent=ACCENTS["source"],
                rows=[
                    Row("axes", "c, z, y, x"),
                    Row("dtype", str(store.arrays[f"{EXAMPLE_WELL}/{EXAMPLE_IMAGE}/0"].dtype)),
                    Row("channels", f"{len(listing.channels)} per round"),
                    Row("pixel", "1 × 0.65 × 0.65 µm"),
                    Row("levels", "4, halved in y and x"),
                    Row("codec", f"{SOURCE_CODEC}, then Deflate"),
                ],
            ),
        ),
        (
            TreeNode("0 1 2 3", 0, 4, count=4 * sum(len(r) for r in listing.rounds.values()), subtitle="levels"),
            dict(
                title="<round>/<level>",
                source=".zarray, chunk files c/z/y/x",
                accent=ACCENTS["source"],
                rows=level_rows(),
                cube=lambda top: side_cube(
                    canvas,
                    top,
                    f"source {EXAMPLE_WELL}/1/0",
                    ["round 1, level 0", f"shape  {d.dims((len(listing.channels), 271, *example))}", f"chunks {d.dims(SOURCE_CHUNKS)}"],
                    example,
                    accent=ACCENTS["source"],
                    grid=(d.grid_lines(example[1], SOURCE_CHUNKS[-1]), d.grid_lines(example[0], SOURCE_CHUNKS[-2])),
                    outlines=fields,
                ),
            ),
        ),
        (
            TreeNode("labels/<name>", 0, 4, count=len(listing.labels), subtitle=f"round {d.span(min(listing.label_rounds), max(listing.label_rounds))} only"),
            dict(
                title="<row>/<col>/1/labels/<name>/<level>",
                source="segmentations",
                accent=ACCENTS["source"],
                cube=lambda top: side_cube(
                    canvas,
                    top,
                    f"source {EXAMPLE_WELL}/1/labels/cells/0",
                    ["round 1, level 0", f"shape  {d.dims((271, *example))}", f"chunks {d.dims(SOURCE_LABEL_CHUNKS)}"],
                    example,
                    accent=ACCENTS["source"],
                    grid=(d.grid_lines(example[1], SOURCE_LABEL_CHUNKS[-1]), d.grid_lines(example[0], SOURCE_LABEL_CHUNKS[-2])),
                    outlines=fields,
                ),
                rows=[
                    Row("names", ", ".join(sorted(listing.labels, key=str.lower))),
                    Row("dtype", str(store.arrays[f"{EXAMPLE_WELL}/{EXAMPLE_IMAGE}/labels/cells/0"].dtype)),
                    Row("shape", label_shape),
                    Row("chunks", d.dims(SOURCE_LABEL_CHUNKS)),
                    Row("stored", f"{sum(listing.label_chunks.values()):,} chunks, 4 levels"),
                    Row("empty files", f"{empty_label:,} chunks", status="removed", tag="empty"),
                ],
            ),
        ),
        (
            TreeNode("tables/", 0, 4, count=len(listing.tables), subtitle="AnnData"),
            dict(
                title="<row>/<col>/<round>/tables/<name>",
                source="AnnData ROI tables",
                accent=ACCENTS["source"],
                rows=[
                    Row("FOV_ROI_table", f"{axis_span(fields_per_well)} rows, rounds {rounds_text(listing.tables['FOV_ROI_table'])}"),
                    Row("well_ROI_table", f"1 row, rounds {rounds_text(listing.tables['well_ROI_table'])}"),
                    Row("embryoRaw_ROI_table", f"1 row, round {rounds_text(listing.tables['embryoRaw_ROI_table'])}"),
                    Row("members", f"{listing.table_members:,}"),
                ],
            ),
        ),
    ]
    tree_figure(canvas, y + 20, entries)
    return canvas


def rounds_text(rounds: set[int]) -> str:
    rounds = sorted(rounds)
    return d.span(rounds[0], rounds[-1])


# Every field's windows


@dataclass
class FieldCrop:
    """One field of a well: its store image, its level-0 window, and its cut on every level."""

    image: str
    name: str
    origin: dict[str, int]
    size: dict[str, int]
    levels: list
    #: The stored array's shape and (y, x) translation at each level.
    shapes: list[tuple[int, ...]]
    translations: list[tuple[float, float]]

    def wider(self, k: int, axis: str) -> bool:
        return self.levels[k].shape["yx".index(axis)] > self.size[axis] // 2**k


def field_crops(store: Store, well: str) -> list[FieldCrop]:
    """Each field's windows, cut again with ``crop_levels`` and checked against the store."""
    extent = well_extent(store, well)
    crops = []
    for image in store.images(well):
        path = f"{well}/{image}"
        annotations = store.groups[path].attrs["annotations"]
        roi = annotations["FOV_ROI_table"]
        scale = image_scales(store, path)
        pixel = scale[0][-1]
        origin = {"y": round(roi["y_micrometer"] / pixel), "x": round(roi["x_micrometer"] / pixel)}
        size = {"y": round(roi["len_y_micrometer"] / pixel), "x": round(roi["len_x_micrometer"] / pixel)}
        cut = crop_levels(origin, size, source_levels(extent), [{"y": s[-2], "x": s[-1]} for s in scale])
        shapes = [store.arrays[f"{path}/{k}"].shape for k in range(len(cut))]
        translations = [tuple(t[-2:]) for t in image_translations(store, path)]
        for k, level in enumerate(cut):
            if [round(level.translation[a], 6) for a in "yx"] != [round(t, 6) for t in translations[k]]:
                raise ValueError(f"{path} level {k}: the store's translation differs from the cut's")
            if level.shape != shapes[k][-2:]:
                raise ValueError(f"{path} level {k}: the store's shape differs from the cut's")
        crops.append(FieldCrop(image, annotations["field"], origin, size, cut, shapes, translations))
    return crops


# The mapping figure


def mapping_figure(listing: Listing, store: Store) -> Canvas:
    well, image = EXAMPLE_WELL, EXAMPLE_IMAGE
    path = f"{well}/{image}"
    attrs = store.groups[path].attrs
    annotations = attrs["annotations"]
    roi = annotations["FOV_ROI_table"]
    field_name = annotations["field"]
    pixel = image_scales(store, path)[0][-1]
    extent = well_extent(store, well)
    crop = next(c for c in field_crops(store, well) if c.image == image)
    origin, size = crop.origin, crop.size
    others = []
    for other in store.images(well):
        r = store.groups[f"{well}/{other}"].attrs["annotations"]["FOV_ROI_table"]
        others.append(window(r, pixel, extent))
    channels = annotations["channels"]
    scene = next(
        t for t in store.groups[well].attrs["ome"]["scene"]["coordinateTransformations"] if t["input"]["path"] == image
    )
    level0 = store.arrays[f"{path}/0"]
    codec = codec_text(level0)

    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Mapping", f"source {well}, field {field_name} → store {path}")
    y = d.legend(
        canvas,
        canvas.margin,
        y + 14,
        [(ACCENTS["source"], "source"), (ACCENTS["store"], "store"), (ACCENTS["highlight"], field_name), ("arrow", "copied")],
    )

    left, right = 24, 960
    lane = y + 30

    # Pixels: one round's well image, with the field's window, onto the field's image.
    src = CubeGeometry(left, lane, 330, 330 * extent[0] / extent[1], 34)
    bottom = d.cube(
        canvas,
        src,
        f"source {well}/1/0",
        [
            f"shape  {d.dims((len(listing.channels), 271, *extent))}",
            f"chunks {d.dims(SOURCE_CHUNKS)}",
            f"codec  {SOURCE_CODEC}",
        ],
        accent=ACCENTS["source"],
        grid=(d.grid_lines(extent[1], SOURCE_CHUNKS[-1]), d.grid_lines(extent[0], SOURCE_CHUNKS[-2])),
        window=window(roi, pixel, extent),
        outlines=[o for o in others if o != window(roi, pixel, extent)],
        source=f"round 1, level 0; {len(others)} fields",
        label_width=420,
    )
    window_panel_x = 470
    wy = lane + 40
    wbottom = d.panel(
        canvas,
        window_panel_x,
        wy,
        360,
        f"{field_name} window",
        [
            Row("y", f"{origin['y']}–{origin['y'] + size['y']}"),
            Row("x", f"{origin['x']}–{origin['x'] + size['x']}"),
            Row("z", "0–271"),
            Row("rounds", "0–3, same window"),
        ],
        accent=ACCENTS["highlight"],
        source=f"{well}/1/tables/FOV_ROI_table",
    )
    x0, y0, x1, y1 = window(roi, pixel, extent)
    d.link(canvas, src.face(x1, (y0 + y1) / 2), (window_panel_x, wy + 40), color=ACCENTS["highlight"])
    dst = CubeGeometry(right, lane + 20, 150, 150, 90)
    shards = level0.shards
    dbottom = d.cube(
        canvas,
        dst,
        f"store {path}/0",
        [
            f"shape  {d.dims(level0.shape)}",
            f"chunks {d.dims(level0.chunks)}",
            f"shards {d.dims(shards)}",
            f"codec  {codec}",
        ],
        accent=ACCENTS["store"],
        source=f"{field_name}, level 0",
        depth_lines=[k / level0.shape[-3] for k in range(shards[-3], level0.shape[-3], shards[-3])],
        label_width=420,
    )
    d.link(canvas, (window_panel_x + 360, wy + 40), dst.middle_left(), label="copied")
    y = max(bottom, wbottom, dbottom) + 40

    # Channels: round r, channel c becomes channel 4r + c.
    y = section(canvas, y, "channels", "round r, channel c → channel 4r + c")
    rounds = sorted({c["round"] for c in channels})
    store_rows = [Row(str(i), f"{c['label']}") for i, c in enumerate(channels)]
    panel_top = y
    round_tops = []
    ry = y
    for r in rounds:
        mine = [c for c in channels if c["round"] == r]
        rows = [Row(f"c {c['source_channel']}", c["label"]) for c in mine]
        round_tops.append(ry)
        ry = d.panel(canvas, left, ry, 330, f"source {mine[0]['source_image']}", rows, accent=ACCENTS["source"], source=f"round {r}, omero.channels") + 10
    store_height = d.panel_height(len(store_rows), source=True)
    sy = panel_top + (ry - 10 - panel_top - store_height) / 2
    d.panel(canvas, right, sy, 360, f"store {path}  omero.channels", store_rows, accent=ACCENTS["store"], source=f"{field_name}, {len(channels)} channels, round order")
    rows_top = sy + 46
    for i, r in enumerate(rounds):
        start = (left + 330, round_tops[i] + d.panel_height(4, source=True) / 2)
        block = rows_top + 4 * i * d.ROW_HEIGHT + 2 * d.ROW_HEIGHT
        d.link(canvas, start, (right, block), label="copied")
    y = max(ry, sy + store_height) + 30

    # Labels: round 1's segmentations, cut to the field.
    y = section(canvas, y, "labels", f"from round {d.span(min(listing.label_rounds), max(listing.label_rounds))}")
    label = "nucleiRaw"
    label0 = store.arrays[f"{path}/labels/{label}/0"]
    src = CubeGeometry(left, y, 240, 240 * extent[0] / extent[1], 26)
    lbottom = d.cube(
        canvas,
        src,
        f"source {well}/1/labels/{label}/0",
        [f"shape  {d.dims((271, *extent))}", f"chunks {d.dims(SOURCE_LABEL_CHUNKS)}", f"dtype  {label0.dtype}"],
        accent=ACCENTS["source"],
        grid=(d.grid_lines(extent[1], 1000), d.grid_lines(extent[0], 1000)),
        window=window(roi, pixel, extent),
        source=f"round 1; one of {len(listing.labels)}: {', '.join(sorted(listing.labels, key=str.lower))}",
        label_width=520,
    )
    dst = CubeGeometry(right, y + 10, 130, 130, 26)
    rbottom = d.cube(
        canvas,
        dst,
        f"store {path}/labels/{label}/0",
        [
            f"shape  {d.dims(label0.shape)}",
            f"chunks {d.dims(label0.chunks)}",
            f"shards {d.dims(label0.shards)}",
            f"codec  {codec_text(label0)}",
        ],
        accent=ACCENTS["store"],
        source=f"{field_name}; {len(store.groups[f'{path}/labels'].attrs['ome']['labels'])} labels, 4 levels each",
        label_width=420,
    )
    lx0, ly0, lx1, ly1 = window(roi, pixel, extent)
    d.link(canvas, src.face(lx1, (ly0 + ly1) / 2), dst.middle_left(), label="copied")
    y = max(lbottom, rbottom) + 30

    # The ROI row: the field's position becomes the image's translation in the well's scene.
    y = section(canvas, y, "position", "FOV_ROI_table row → scene translation")
    roi_rows = [Row(k, f"{v:g}") for k, v in roi.items() if not k.startswith("z") and not k.startswith("len_z")]
    pbottom = d.panel(canvas, left, y, 420, f"source {field_name} row", roi_rows, accent=ACCENTS["source"], source=f"{well}/<round>/tables/FOV_ROI_table")
    translation = scene["translation"]
    scene_rows = [
        Row("input", f"intrinsic, path {scene['input']['path']}"),
        Row("output", scene["output"]["name"]),
        Row("translation", "[" + ", ".join(f"{t:g}" for t in translation) + "]"),
        Row("axes", "c, z, y, x  (µm)"),
    ]
    sbottom = d.panel(canvas, right, y, 420, f"store {well}  ome.scene", scene_rows, accent=ACCENTS["store"], source="one translation per image")
    kept_rows = [Row("FOV_ROI_table", "row kept as is"), Row("field", field_name)]
    kbottom = d.panel(canvas, right, sbottom + 10, 420, f"store {path}  annotations", kept_rows, accent=ACCENTS["store"])
    d.link(canvas, (left + 420, y + 70), (right, y + 46 + 2.5 * d.ROW_HEIGHT), label="copied")
    d.link(canvas, (left + 420, y + 90), (right, sbottom + 10 + 30 + d.ROW_HEIGHT / 2), label="kept")
    y = max(pbottom, kbottom)

    return canvas


def window(roi: dict, pixel: float, extent: tuple[int, int]) -> d.Window:
    """A ROI row's rectangle as fractions of its well image, ``(x0, y0, x1, y1)``."""
    x0 = roi["x_micrometer"] / pixel / extent[1]
    y0 = roi["y_micrometer"] / pixel / extent[0]
    return (x0, y0, x0 + roi["len_x_micrometer"] / pixel / extent[1], y0 + roi["len_y_micrometer"] / pixel / extent[0])


def section(canvas: Canvas, y: float, title: str, subtitle: str) -> float:
    """A rule across the figure with a section title above it."""
    canvas.add(f'<line x1="24" y1="{y:.1f}" x2="{canvas.width - 24}" y2="{y:.1f}" stroke="{d.RULE}"/>', y)
    canvas.text(24, y + 22, title, size=14, weight="bold")
    canvas.text(24 + d.text_width(title, 14, bold=True) + 12, y + 22, subtitle, size=12, color=d.MUTED)
    return y + 40


# The crops figure


CROPS_WIDTH = 1560
WELL_WIDTH = 900
CROP_PANEL_X = 1080
CROP_PANEL_WIDTH = 456


def crops_figure(listing: Listing, store: Store) -> Canvas:
    well = EXAMPLE_WELL
    extent = well_extent(store, well)
    levels = source_levels(extent)
    crops = field_crops(store, well)
    canvas = Canvas(CROPS_WIDTH)
    y = d.heading(canvas, "Crops", f"source {well}/<round>/<level> → store {well}/<image>/<level>, every field, every level")
    y = d.legend(
        canvas,
        canvas.margin,
        y + 14,
        [
            (ACCENTS["source"], "source well image"),
            (ACCENTS["highlight"], "field window"),
            (ACCENTS["store"], "store image level"),
            ("arrow", "copied"),
            ("changed", "wider"),
        ],
    )
    y += 24
    for k, (height, width) in enumerate(levels):
        f = 2**k
        per_pixel = "1 level-0 pixel" if f == 1 else f"{f} × {f} level-0 pixels"
        y = section(canvas, y, f"level {k}", f"{height} × {width} px, chunks 1000 × 1000, {per_pixel} per pixel")
        y = crop_level(canvas, y, well, k, (height, width), crops) + 30
    y = section(canvas, y, "windows", "start–stop  size, in each level's pixels; field edge = 2^k · start + r")
    y = d.legend(
        canvas,
        canvas.margin,
        y,
        [(d.RULE, "level-0 pixels outside the field"), (ACCENTS["highlight"], "inside the field"), ("changed", "wider")],
    )
    crop_table(canvas, y + 16, crops)
    return canvas


def crop_level(canvas: Canvas, top: float, well: str, k: int, extent: tuple[int, int], crops: list[FieldCrop]) -> float:
    """One level of the source well image to scale, with every field's window, and the store level each becomes."""
    scale = WELL_WIDTH / extent[1]
    x0, y0 = 24.0, top + 22
    canvas.text(x0, top + 10, f"source {well}/<round>/{k}, and {well}/1/labels/<name>/{k}", size=11, color=d.MUTED)
    parts = [
        f'<rect x="{x0}" y="{y0:.1f}" width="{WELL_WIDTH}" height="{extent[0] * scale:.1f}" fill="{d.FACE_FILL}" '
        f'stroke="{ACCENTS["source"]}" stroke-width="1.4"/>'
    ]
    for fx in d.grid_lines(extent[1], SOURCE_CHUNKS[-1]):
        parts.append(
            f'<line x1="{x0 + fx * WELL_WIDTH:.1f}" y1="{y0:.1f}" x2="{x0 + fx * WELL_WIDTH:.1f}" '
            f'y2="{y0 + extent[0] * scale:.1f}" stroke="{d.RULE}"/>'
        )
    for fy in d.grid_lines(extent[0], SOURCE_CHUNKS[-2]):
        parts.append(
            f'<line x1="{x0}" y1="{y0 + fy * extent[0] * scale:.1f}" x2="{x0 + WELL_WIDTH}" '
            f'y2="{y0 + fy * extent[0] * scale:.1f}" stroke="{d.RULE}"/>'
        )
    bottom = canvas.add("\n".join(parts), y0 + extent[0] * scale)
    # The windows are drawn after the arrows, so an arrow passing behind another field leaves it readable.
    boxes, parts = {}, []
    for crop in crops:
        (ys, ye), (xs, xe) = crop.levels[k].window["y"], crop.levels[k].window["x"]
        box = (x0 + xs * scale, y0 + ys * scale, (xe - xs) * scale, (ye - ys) * scale)
        boxes[crop.image] = box
        parts.append(
            f'<rect x="{box[0]:.1f}" y="{box[1]:.1f}" width="{box[2]:.1f}" height="{box[3]:.1f}" fill="{d.BACKGROUND}"/>'
        )
        parts.append(
            f'<rect x="{box[0]:.1f}" y="{box[1]:.1f}" width="{box[2]:.1f}" height="{box[3]:.1f}" '
            f'fill="{ACCENTS["highlight"]}" fill-opacity="0.18" stroke="{ACCENTS["highlight"]}" stroke-width="1.6"/>'
        )
        label = f"{crop.name} → {crop.image}"
        if crop.wider(k, "y") or crop.wider(k, "x"):
            parts.append(
                f'<rect x="{box[0] + 1:.1f}" y="{box[1] + 1:.1f}" width="{d.text_width(label, 11, mono=True, bold=True) + 6:.1f}" '
                f'height="17" fill="{d.STATUS["changed"].fill}"/>'
            )
        parts.append(d.text(box[0] + 4, box[1] + 14, label, size=11, weight="bold", mono=True))
    py = top
    for crop in sorted(crops, key=lambda c: c.origin["y"] + c.size["y"] / 2):
        level = crop.levels[k]
        rows = []
        for axis in "yx":
            start, stop = level.window[axis]
            wider = crop.wider(k, axis)
            rows.append(
                Row(
                    f"window {axis}",
                    f"{start}–{stop}  {stop - start} px",
                    status="changed" if wider else None,
                    tag="wider" if wider else None,
                )
            )
        ty, tx = crop.translations[k]
        rows.append(Row("shape", d.dims(crop.shapes[k])))
        rows.append(Row("translation", f"y {micrometers(ty)}, x {micrometers(tx)}"))
        panel_bottom = d.panel(
            canvas,
            CROP_PANEL_X,
            py,
            CROP_PANEL_WIDTH,
            f"store {well}/{crop.image}/{k}",
            rows,
            source=crop.name,
        )
        bx, by, bw, bh = boxes[crop.image]
        d.link(canvas, (bx + bw, by + bh / 2), (CROP_PANEL_X, py + 20), color=ACCENTS["highlight"], stroke=1.2)
        py = panel_bottom + 8
    canvas.add("\n".join(parts), bottom)
    return max(bottom, py)


def crop_table(canvas: Canvas, top: float, crops: list[FieldCrop]) -> float:
    """Every field's window at every level, with the coarse pixel that holds each field edge."""
    levels = range(len(crops[0].levels))
    widths = [130, 190] + [370] * (len(levels) - 1)
    header = ["field → image"] + [f"level {k}" if k == 0 else f"level {k}  ({2**k} × {2**k})" for k in levels]
    rows = []
    for crop in crops:
        row = [d.Cell([crop.name, f"→ image {crop.image}"])]
        for k in levels:
            lines = []
            for axis in "yx":
                start, stop = crop.levels[k].window[axis]
                lines.append(f"{axis} {start}–{stop}  {stop - start}")
            wider = crop.wider(k, "y") or crop.wider(k, "x")
            row.append(d.Cell(lines, status="changed" if wider else None))
        rows.append(row)

    def glyph(i: int, j: int, x: float, y: float) -> str:
        """The coarse pixel that holds the field's first row or column, split into its level-0 pixels."""
        k = j - 1
        if k < 1:
            return ""
        crop = crops[i]
        f = 2**k
        parts = []
        for n, axis in enumerate("yx"):
            o = crop.origin[axis]
            start, r = divmod(o, f)
            gx, gy = x + 128, y + 8 + n * d.CELL_LINE
            cell = 48 / f
            for c in range(f):
                color = d.RULE if c < r else ACCENTS["highlight"]
                parts.append(f'<rect x="{gx + c * cell:.1f}" y="{gy:.1f}" width="{cell:.1f}" height="12" fill="{color}"/>')
            parts.append(f'<rect x="{gx:.1f}" y="{gy:.1f}" width="48" height="12" fill="none" stroke="{d.INK}" stroke-width="1"/>')
            parts.append(d.text(gx + 56, gy + 10, f"{o} = {f}·{start} + {r}", size=11, mono=True, color=d.MUTED))
        return "\n".join(parts)

    return d.table(canvas, 24, top, widths, header, rows, draw=glyph)


# The store figure


def store_figure(listing: Listing, store: Store) -> Canvas:
    root_attrs = store.root.attrs.asdict()
    plate = root_attrs["ome"]["plate"]
    source = root_attrs["source"]
    wells = store.wells()
    images = store.image_paths()
    image_attrs = {p: store.groups[p].attrs.asdict() for p in images}
    label_groups = [p for p in store.groups if re.fullmatch(r"[A-Z]/\d\d/\d+/labels/[^/]+", p)]
    label_names = sorted({p.rsplit("/", 1)[1] for p in label_groups}, key=str.lower)
    rows_groups = [p for p in store.groups if re.fullmatch(r"[A-Z]", p)]
    row_attrs = {k for p in rows_groups for k in store.groups[p].attrs}
    image_arrays = defaultdict(list)
    label_arrays = defaultdict(list)
    for p, a in store.arrays.items():
        if m := re.fullmatch(r"[A-Z]/\d\d/\d+/(\d+)", p):
            image_arrays[int(m[1])].append(a)
        elif m := re.fullmatch(r"[A-Z]/\d\d/\d+/labels/[^/]+/(\d+)", p):
            label_arrays[int(m[1])].append(a)
    empty = sum(len(a["annotations"]["empty_source_chunks"]) for a in image_attrs.values())
    unreadable = sum(len(a["annotations"]["unreadable_source_chunks"]) for a in image_attrs.values())
    images_with_unreadable = sum(bool(a["annotations"]["unreadable_source_chunks"]) for a in image_attrs.values())
    images_with_empty = sum(bool(a["annotations"]["empty_source_chunks"]) for a in image_attrs.values())
    translations = sum(len(store.groups[w].attrs["ome"]["scene"]["coordinateTransformations"]) for w in wells)
    example = image_attrs[f"{EXAMPLE_WELL}/{EXAMPLE_IMAGE}"]
    axes = [a["name"] for a in example["ome"]["multiscales"][0]["coordinateSystems"][0]["axes"]]
    level_scale = image_scales(store, f"{EXAMPLE_WELL}/{EXAMPLE_IMAGE}")
    nonzero = sum(any(t != 0 for t in tr) for p in images for tr in image_translations(store, p)[1:])
    well_annotations = store.groups[wells[0]].attrs["annotations"]

    def level_rows(arrays: dict[int, list[zarr.Array]]) -> list[Row]:
        rows = []
        for k in sorted(arrays):
            rows.append(Row(f"level {k}", f"{shape_span([a.shape for a in arrays[k]])}   {len(arrays[k]):,} arrays"))
        first = arrays[0][0]
        rows.append(Row("chunks", plane_text(first.chunks[:-2]) + " plane"))
        rows.append(Row("shards", plane_text(first.shards[:-2]) + " planes"))
        rows.append(Row("dtype", str(first.dtype)))
        rows.append(Row("codec", codec_text(first)))
        return rows

    def cube_beside(arrays: dict[int, list[zarr.Array]], title: str):
        """Level 0 as one channel's block: the face is one chunk, the depth is z cut into shards."""
        a = arrays[0][0]
        planes, depth = a.shape[-3], a.shards[-3]
        shards = math.ceil(planes / depth)
        lines = [
            f"face   1 chunk, {d.dims(a.chunks[-2:])}",
            f"z      {planes} planes, {shards} shard{'s' if shards > 1 else ''}",
            f"shard  {depth} planes, {depth} chunks",
        ]
        if a.ndim == 4:
            lines.append(f"c      {a.shape[0]}, a column each")
        return lambda top: side_cube(
            canvas, top, title, lines, (1, 1), width=110, depth=90,
            depth_lines=[k / planes for k in range(depth, planes, depth)],
        )

    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "Store", f"{REPO.removeprefix('arraylake://')}  main @ {store.snapshot}")
    y = d.legend(canvas, canvas.margin, y + 14, [(ACCENTS["store"], "store"), ("added", "added"), ("changed", "changed")])
    entries = [
        (
            TreeNode("/", 0, 0, subtitle="plate"),
            dict(
                title="/  zarr.json",
                source="from /.zattrs and the zip's listing",
                rows=[
                    Row("ome.version", root_attrs["ome"]["version"], status="changed", tag="upgraded"),
                    Row("ome.plate", f"{plate['name']}, {len(plate['wells'])} wells, field_count {plate['field_count']}"),
                    Row("  rows, columns", f"{len(plate['rows'])} × {len(plate['columns'])}"),
                    Row("  acquisitions", "none" if "acquisitions" not in plate else str(len(plate["acquisitions"])), status="changed", tag="dropped"),
                    Row("source", f"study {source['study'].rsplit('/', 1)[-1]}, {source['license']}", status="added", tag="added"),
                    Row("  url", source["url"].rsplit("/", 1)[-1]),
                    Row("  rounds", f"0–2: 4 channels; 3: per well ({len(source['rounds']['3'])})"),
                    Row("  source_acquisitions", ", ".join(str(a) for a in source["source_acquisitions"])),
                ],
            ),
        ),
        (
            TreeNode("<row>", 0, 1, count=len(rows_groups), subtitle="row group"),
            dict(title="<row>", source="from <row>/.zgroup", rows=[Row("attributes", ", ".join(sorted(row_attrs)) or "none")]),
        ),
        (
            TreeNode("<col>", 0, 2, count=len(wells), subtitle="well"),
            dict(
                title="<row>/<col>",
                source="from <row>/<col>/.zattrs and tables/*_ROI_table",
                rows=[
                    Row("ome.well", f"images {axis_span(len(store.images(w)) for w in wells)} per well"),
                    Row("ome.scene", f"system 'well', {translations} translations", status="added", tag="added"),
                    Row("annotations", ", ".join(k for k in well_annotations if k != "source_url")),
                ],
            ),
        ),
        (
            TreeNode("<image>", 0, 3, count=len(images), subtitle="one field"),
            dict(
                title="<row>/<col>/<image>",
                source="from <round>/.zattrs ×4 and the field's FOV_ROI_table row",
                rows=[
                    Row("ome.multiscales", f"4 levels, axes {', '.join(axes)}", status="changed", tag="cut"),
                    Row("  scale 0", d.dims(f"{s:g}" for s in level_scale[0]) + " µm"),
                    Row("  translations", f"{nonzero} nonzero, levels 1–3", status="added", tag="added"),
                    Row("omero.channels", f"{len(example['ome']['omero']['channels'])}, round order", status="changed", tag="merged"),
                    Row("annotations", "well, field, channels, FOV_ROI_table"),
                    Row("  empty_source_chunks", f"{empty:,} in {images_with_empty} images"),
                    Row("  unreadable_source_chunks", f"{unreadable:,} in {images_with_unreadable} images"),
                ],
            ),
        ),
        (
            TreeNode("0 1 2 3", 0, 4, count=sum(len(v) for v in image_arrays.values()), subtitle="image levels"),
            dict(
                title="<image>/<level>",
                source="from <row>/<col>/0–3/<level>, cut to the field",
                rows=level_rows(image_arrays),
                cube=cube_beside(image_arrays, "<image>/0"),
            ),
        ),
        (
            TreeNode("labels", 0, 4, count=sum(1 for p in store.groups if p.endswith("/labels")), subtitle="labels group"),
            dict(
                title="<image>/labels",
                source="from <row>/<col>/1/labels/.zattrs",
                rows=[Row("ome.labels", ", ".join(label_names))],
            ),
        ),
        (
            TreeNode("<name>", 0, 5, count=len(label_groups), subtitle="label image"),
            dict(
                title="<image>/labels/<name>",
                source="from <row>/<col>/1/labels/<name>/.zattrs",
                rows=[
                    Row("ome.image-label", "source ../../"),
                    Row("ome.multiscales", "4 levels, axes z, y, x"),
                    Row("annotations", "round 1, source_label"),
                ],
            ),
        ),
        (
            TreeNode("0 1 2 3", 0, 6, count=sum(len(v) for v in label_arrays.values()), subtitle="label levels"),
            dict(
                title="<image>/labels/<name>/<level>",
                source="from <row>/<col>/1/labels/<name>/<level>, cut to the field",
                rows=level_rows(label_arrays),
                cube=cube_beside(label_arrays, "<name>/0"),
            ),
        ),
    ]
    tree_figure(canvas, y + 20, entries)
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--directory-cache", type=Path, default=ingest.DIRECTORY_CACHE)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    listing = read_listing(args.directory_cache)
    store = read_store(args.repo, args.branch)
    args.out.mkdir(parents=True, exist_ok=True)
    figures = (("source", source_figure), ("mapping", mapping_figure), ("crops", crops_figure), ("store", store_figure))
    for name, figure in figures:
        size = figure(listing, store).write(args.out / f"{name}.svg")
        print(f"{name}.svg  {size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
