"""Draw the zebrafish ingest: the source zip, how it maps onto the store, and the store.

    ARRAYLAKE_TOKEN=... uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/diagrams.py \\
        --directory-cache <central-directory.parquet>

Writes ``source.svg``, ``mapping.svg`` and ``store.svg`` into ``diagrams/``
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
shape) are constants below, each from PLAN.md's survey of the zip.

The mapping figure cuts its field's coarse windows again with
:func:`minus80.ome.crop.crop_levels` and stops if they differ from the
shapes and translations in the store, so the inset cannot drift from the data.

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

# Facts about the source that neither the directory nor the store records.
# PLAN.md, "What is in the zip", has the survey they come from.
SOURCE_VERSION = "0.4"
SOURCE_CHUNKS = (1, 10, 1000, 1000)
SOURCE_LABEL_CHUNKS = (10, 1000, 1000)
SOURCE_CODEC = "blosc lz4 5, shuffle"

#: The well and field the mapping figure follows. FOV_2 of C/02 starts off the coarse grid on
#: both axes, so its coarse windows are wider than 1000 / 2^k.
EXAMPLE_WELL = "C/02"
EXAMPLE_IMAGE = "1"
EXAMPLE_LEVEL = 3


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


def side_cube(canvas: Canvas, top: float, title: str, lines: list[str], aspect: tuple[float, float], **style) -> float:
    """A cube for one array, in the column right of the panels, its face ``aspect`` (y, x) in proportion."""
    width = 150
    height = width * aspect[0] / aspect[1]
    return d.cube(
        canvas, CubeGeometry(CUBE_X, top + 8, width, height, 26), title, lines, label_width=WIDTH - CUBE_X - 24, **style
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
                    f"{EXAMPLE_WELL}/1/0",
                    [f"shape  {d.dims((len(listing.channels), 271, *example))}", f"chunks {d.dims(SOURCE_CHUNKS)}"],
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
                    f"{EXAMPLE_WELL}/1/labels/cells/0",
                    [f"shape  {d.dims((271, *example))}", f"chunks {d.dims(SOURCE_LABEL_CHUNKS)}"],
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


# The mapping figure


def mapping_figure(listing: Listing, store: Store) -> Canvas:
    well, image = EXAMPLE_WELL, EXAMPLE_IMAGE
    path = f"{well}/{image}"
    attrs = store.groups[path].attrs
    annotations = attrs["annotations"]
    roi = annotations["FOV_ROI_table"]
    field_name = annotations["field"]
    scale = image_scales(store, path)
    pixel = scale[0][-1]
    extent = well_extent(store, well)
    levels = source_levels(extent)
    origin = {"y": round(roi["y_micrometer"] / pixel), "x": round(roi["x_micrometer"] / pixel)}
    size = {"y": round(roi["len_y_micrometer"] / pixel), "x": round(roi["len_x_micrometer"] / pixel)}
    cut = crop_levels(origin, size, levels, [{"y": s[-2], "x": s[-1]} for s in scale])
    stored_translations = image_translations(store, path)
    for k, level in enumerate(cut):
        if [round(level.translation[a], 6) for a in "yx"] != [round(t, 6) for t in stored_translations[k][-2:]]:
            raise ValueError(f"{path} level {k}: the store's translation differs from the cut's")
        if level.shape != store.arrays[f"{path}/{k}"].shape[-2:]:
            raise ValueError(f"{path} level {k}: the store's shape differs from the cut's")
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
    y = d.heading(canvas, "Mapping", f"{well}, field {field_name} → {path}")
    y = d.legend(
        canvas,
        canvas.margin,
        y + 14,
        [(ACCENTS["source"], "source"), (ACCENTS["store"], "store"), (ACCENTS["highlight"], field_name), ("arrow", "copied"), ("changed", "wider")],
    )

    left, right = 24, 960
    lane = y + 30

    # Pixels: one round's well image, with the field's window, onto the field's image.
    src = CubeGeometry(left, lane, 330, 330 * extent[0] / extent[1], 34)
    bottom = d.cube(
        canvas,
        src,
        f"{well}/1/0",
        [
            f"shape  {d.dims((len(listing.channels), 271, *extent))}",
            f"chunks {d.dims(SOURCE_CHUNKS)}",
            f"codec  {SOURCE_CODEC}",
        ],
        accent=ACCENTS["source"],
        grid=(d.grid_lines(extent[1], SOURCE_CHUNKS[-1]), d.grid_lines(extent[0], SOURCE_CHUNKS[-2])),
        window=window(roi, pixel, extent),
        outlines=[o for o in others if o != window(roi, pixel, extent)],
        source=f"round 1, level 0, {len(others)} fields",
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
    dst = CubeGeometry(right, lane + 20, 170, 170, 30)
    shards = level0.shards
    dbottom = d.cube(
        canvas,
        dst,
        f"{path}/0",
        [
            f"shape  {d.dims(level0.shape)}",
            f"chunks {d.dims(level0.chunks)}",
            f"shards {d.dims(shards)}",
            f"codec  {codec}",
        ],
        accent=ACCENTS["store"],
        source=f"field {field_name}, level 0",
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
        ry = d.panel(canvas, left, ry, 330, f"{mine[0]['source_image']}", rows, accent=ACCENTS["source"], source=f"round {r}, omero.channels") + 10
    store_height = d.panel_height(len(store_rows), source=True)
    sy = panel_top + (ry - 10 - panel_top - store_height) / 2
    d.panel(canvas, right, sy, 360, f"{path}  omero.channels", store_rows, accent=ACCENTS["store"], source=f"{len(channels)} channels, round order")
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
        f"{well}/1/labels/{label}/0",
        [f"shape  {d.dims((271, *extent))}", f"chunks {d.dims(SOURCE_LABEL_CHUNKS)}", f"dtype  {label0.dtype}"],
        accent=ACCENTS["source"],
        grid=(d.grid_lines(extent[1], 1000), d.grid_lines(extent[0], 1000)),
        window=window(roi, pixel, extent),
        source=f"one of {len(listing.labels)}: {', '.join(sorted(listing.labels, key=str.lower))}",
        label_width=520,
    )
    dst = CubeGeometry(right, y + 10, 130, 130, 26)
    rbottom = d.cube(
        canvas,
        dst,
        f"{path}/labels/{label}/0",
        [
            f"shape  {d.dims(label0.shape)}",
            f"chunks {d.dims(label0.chunks)}",
            f"shards {d.dims(label0.shards)}",
            f"codec  {codec_text(label0)}",
        ],
        accent=ACCENTS["store"],
        source=f"{len(store.groups[f'{path}/labels'].attrs['ome']['labels'])} labels, 4 levels each",
        label_width=420,
    )
    lx0, ly0, lx1, ly1 = window(roi, pixel, extent)
    d.link(canvas, src.face(lx1, (ly0 + ly1) / 2), dst.middle_left(), label="copied")
    y = max(lbottom, rbottom) + 30

    # The ROI row: the field's position becomes the image's translation in the well's scene.
    y = section(canvas, y, "position", "FOV_ROI_table row → scene translation")
    roi_rows = [Row(k, f"{v:g}") for k, v in roi.items() if not k.startswith("z") and not k.startswith("len_z")]
    pbottom = d.panel(canvas, left, y, 420, f"{field_name}", roi_rows, accent=ACCENTS["source"], source=f"{well}/<round>/tables/FOV_ROI_table")
    translation = scene["translation"]
    scene_rows = [
        Row("input", f"intrinsic, path {scene['input']['path']}"),
        Row("output", scene["output"]["name"]),
        Row("translation", "[" + ", ".join(f"{t:g}" for t in translation) + "]"),
        Row("axes", "c, z, y, x  (µm)"),
    ]
    sbottom = d.panel(canvas, right, y, 420, f"{well}  ome.scene", scene_rows, accent=ACCENTS["store"], source="one translation per image")
    kept_rows = [Row("FOV_ROI_table", "row kept as is"), Row("field", field_name)]
    kbottom = d.panel(canvas, right, sbottom + 10, 420, f"{path}  annotations", kept_rows, accent=ACCENTS["store"])
    d.link(canvas, (left + 420, y + 70), (right, y + 46 + 2.5 * d.ROW_HEIGHT), label="copied")
    d.link(canvas, (left + 420, y + 90), (right, sbottom + 10 + 30 + d.ROW_HEIGHT / 2), label="kept")
    y = max(pbottom, kbottom) + 30

    # One coarse level: the window covers every coarse pixel the field touches.
    k = EXAMPLE_LEVEL
    f = 2**k
    level = cut[k]
    y = section(canvas, y, f"level {k}", f"{f} × {f} blocks; window covers every coarse pixel the field touches")
    ibottom = coarse_strip(canvas, left, y, origin["x"], f, level.window["x"][0])
    stored = store.arrays[f"{path}/{k}"]
    level_rows = [
        Row("source", f"{well}/1/{k}  {d.dims((len(listing.channels), 271, *levels[k]))}"),
        Row("y", f"{level.window['y'][0]}–{level.window['y'][1]}  ({level.shape[0]} px)", status="changed", tag="wider" if level.shape[0] > 1000 // f else None),
        Row("x", f"{level.window['x'][0]}–{level.window['x'][1]}  ({level.shape[1]} px)", status="changed", tag="wider" if level.shape[1] > 1000 // f else None),
        Row("translation", f"y {micrometers(level.translation['y'])}, x {micrometers(level.translation['x'])}"),
        Row("store", f"{path}/{k}  {d.dims(stored.shape)}"),
    ]
    tbottom = d.panel(canvas, right, y, 420, f"{path}/{k}", level_rows, accent=ACCENTS["store"], source="ome.multiscales, dataset 3")
    d.link(canvas, (left + 4 * 2**k * 10 + 20, y + 85), (right, y + 46 + 2.5 * d.ROW_HEIGHT), label="copied")
    y = max(ibottom, tbottom)
    return canvas


def window(roi: dict, pixel: float, extent: tuple[int, int]) -> d.Window:
    """A ROI row's rectangle as fractions of its well image, ``(x0, y0, x1, y1)``."""
    x0 = roi["x_micrometer"] / pixel / extent[1]
    y0 = roi["y_micrometer"] / pixel / extent[0]
    return (x0, y0, x0 + roi["len_x_micrometer"] / pixel / extent[1], y0 + roi["len_y_micrometer"] / pixel / extent[0])


def section(canvas: Canvas, y: float, title: str, subtitle: str) -> float:
    """A rule across the figure with a section title above it."""
    canvas.add(f'<line x1="24" y1="{y:.1f}" x2="{WIDTH - 24}" y2="{y:.1f}" stroke="{d.RULE}"/>', y)
    canvas.text(24, y + 22, title, size=14, weight="bold")
    canvas.text(24 + d.text_width(title, 14, bold=True) + 12, y + 22, subtitle, size=12, color=d.MUTED)
    return y + 40


def coarse_strip(canvas: Canvas, x: float, y: float, origin: int, factor: int, first: int) -> float:
    """Level-0 pixels around a field's left edge, under the coarse pixels that hold them.

    Shows the coarse pixel before the window, the first ones in it, and where
    the field's first level-0 pixel falls inside the first.
    """
    px = 10.0
    start = (first - 1) * factor
    count = 4 * factor
    parts = []
    top = y + 34
    for j in range(4):
        cx = x + j * factor * px
        inside = j > 0
        parts.append(
            f'<rect x="{cx:.1f}" y="{top:.1f}" width="{factor * px:.1f}" height="34" '
            f'fill="{d.FACE_FILL if inside else d.BACKGROUND}" stroke="{ACCENTS["store"] if inside else d.RULE}" stroke-width="1.5"/>'
        )
        parts.append(d.text(cx + factor * px / 2, top + 21, str(first - 1 + j), size=11.5, mono=True, anchor="middle", color=d.INK if inside else d.MUTED))
    row = top + 50
    for i in range(count):
        p = start + i
        in_field = p >= origin
        parts.append(
            f'<rect x="{x + i * px:.1f}" y="{row:.1f}" width="{px - 1:.1f}" height="16" '
            f'fill="{ACCENTS["highlight"] if in_field else d.RULE}" fill-opacity="{0.55 if in_field else 0.6}"/>'
        )
    field_x = x + (origin - start) * px
    parts.append(f'<line x1="{field_x:.1f}" y1="{top - 6:.1f}" x2="{field_x:.1f}" y2="{row + 22:.1f}" stroke="{ACCENTS["highlight"]}" stroke-width="2"/>')
    parts.append(d.text(field_x + 4, row + 52, f"field x {origin}", size=11.5, mono=True, color=ACCENTS["highlight"]))
    first_x = x + factor * px
    parts.append(d.text(first_x, top - 8, f"coarse x {first}  =  level 0 x {first * factor}", size=11.5, mono=True))
    parts.append(d.text(x, row + 34, f"level 0 x {start}", size=11.5, mono=True, color=d.MUTED))
    canvas.text(x, y + 10, f"coarse pixels (level {round(math.log2(factor))})", size=11, color=d.MUTED)
    return canvas.add("\n".join(parts), row + 58)


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
        a = arrays[0][0]
        lines = [f"shape  {d.dims(a.shape)}", f"chunks {d.dims(a.chunks)}", f"shards {d.dims(a.shards)}"]
        return lambda top: side_cube(canvas, top, title, lines, (1, 1))

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
    for name, figure in (("source", source_figure), ("mapping", mapping_figure), ("store", store_figure)):
        size = figure(listing, store).write(args.out / f"{name}.svg")
        print(f"{name}.svg  {size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
