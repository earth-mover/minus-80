"""What the S-BIAD3228 zip's layout is, computed from its central directory and a well's raw metadata.

Every function here is pure: it takes the tables and dicts :mod:`inputs`
loaded and returns new ones, and fetches nothing. The terms are the ones the
tools and the ingest's PLAN.md use:

- **member**: one file inside the zip. Its name is a Zarr v2 key.
- **span**: a member's bytes in the zip, from its local header to the next
  member's local header (``start`` to ``end``); it holds the local header,
  the data and the data descriptor. Spans tile the file with nothing between
  them.
- **chunk**: a member that holds pixels, one source Zarr chunk: 1 channel x
  10 planes x 1000 x 1000 pixels for an image, 10 planes x 1000 x 1000 for a
  label. ``C/02/2/0/1/13/0/7`` is well ``C/02``, round 2, pyramid level 0,
  then the chunk's index on each axis of ``(c, z, y, x)``: channel 1,
  z index 13 (planes 130 to 139), grid row 0 and column 7.
- **z index**: a chunk's index along z, which covers 10 planes.
- **padding**: a chunk that is all zeros, recognizable from its size alone:
  an 80,897-byte Blosc frame for an image, 161,455 for a label. Four image
  chunks are 161,455 bytes and hold real pixels, so the size is checked
  against the chunk's kind.
- **empty**: a member with no bytes at all; it reads as 0. Empty members and
  members cut short (a Blosc frame shorter than its header says) are the
  **damaged** members; only empty ones show in the directory.
- **run**: consecutive members of one array, or of one metadata folder. The
  zip stores each well's members contiguously and, within a well, one array
  after another.
- **(channel, z index) group**: consecutive chunks of one run that share a
  channel and z index; in S-BIAD3228 each group is one z index of one
  channel across every stored grid cell.
- **region**: a well's bytes, from its first member's span to its last.
- **range**: one range request for consecutive bytes of the zip, such as
  64 MiB of a well's region.
- **grid cell**: a chunk's (y, x) position in its level's chunk grid. A field
  of view is placed anywhere on its well's image, so it overlaps one to four
  grid cells at each level.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np
import polars as pl
import pyarrow as pa

from minus80.parsers.zip.directory import member_spans

CHUNK_PIXELS = 1000
PLANES_PER_CHUNK = 10
LEVELS = 4
#: The Blosc frame size of an all-zero chunk, by kind.
PADDING_FRAME = {"image": 80_897, "label": 161_455}
IMAGE_CHUNK = r"^([B-G]/0[2-7])/(\d)/(\d)/(\d+)/(\d+)/(\d+)/(\d+)$"
LABEL_CHUNK = r"^([B-G]/0[2-7])/(\d)/labels/([^/]+)/(\d)/(\d+)/(\d+)/(\d+)$"
#: Chunk flags, one byte per chunk, as the interactive map reads them.
FLAG_EMPTY, FLAG_PADDING, FLAG_TO_DECODE = 1, 2, 4


class ReadSetting(NamedTuple):
    """Where a well is read from, and how fast, for estimating request time."""

    name: str
    latency_s: float
    concurrency: int
    #: Bytes per second across all requests in flight.
    throughput: float


#: Measured on 2026-10-27: R2 from AWS us-east-1 at 64 requests in flight,
#: and EBI from AWS London at 16, EBI's limit.
READ_SETTINGS = (
    ReadSetting("R2 in-region", 0.100, 64, 584e6),
    ReadSetting("EBI from London", 0.370, 16, 280e6),
)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def describe_members(directory: pa.Table) -> pl.DataFrame:
    """The central directory in file order, with each member's span and its name split into what it addresses.

    Adds ``start`` and ``end`` (the member's span, from
    :func:`minus80.parsers.zip.directory.member_spans`), ``kind`` (``image``,
    ``label``, ``table`` or ``meta``), the well, round, label name, level and
    chunk index (``c``, ``z``, ``y``, ``x``) where the name has them,
    ``array`` (the array or folder the member belongs to), and the ``empty``
    and ``padding`` flags.
    """
    spans = member_spans(directory)
    ordered = directory.take(spans["entry"]).append_column("start", spans["start"]).append_column("end", spans["end"])
    name = pl.col("name")
    image = name.str.extract_groups(IMAGE_CHUNK)
    label = name.str.extract_groups(LABEL_CHUNK)
    members = pl.from_arrow(ordered).with_columns(
        kind=pl.when(name.str.contains(IMAGE_CHUNK)).then(pl.lit("image"))
        .when(name.str.contains(LABEL_CHUNK)).then(pl.lit("label"))
        .when(name.str.contains("/tables/")).then(pl.lit("table"))
        .otherwise(pl.lit("meta")),
        well=pl.coalesce(image.struct["1"], label.struct["1"], name.str.extract(r"^([B-G]/0[2-7])/")),
        round=pl.coalesce(image.struct["2"], label.struct["2"]).cast(pl.Int8),
        label=label.struct["3"],
        level=pl.coalesce(image.struct["3"], label.struct["4"]).cast(pl.Int8),
        c=image.struct["4"].cast(pl.Int8),
        z=pl.coalesce(image.struct["5"], label.struct["5"]).cast(pl.Int16),
        y=pl.coalesce(image.struct["6"], label.struct["6"]).cast(pl.Int16),
        x=pl.coalesce(image.struct["7"], label.struct["7"]).cast(pl.Int16),
    )
    folder = name.str.extract(r"^(.*)/[^/]+$").fill_null("")
    table_group = name.str.extract(r"^(.*/tables/[^/]+)/")
    return members.with_columns(
        array=pl.when(pl.col("kind") == "image").then(pl.format("{}/{}/{}", "well", "round", "level"))
        .when(pl.col("kind") == "label").then(pl.format("{}/{}/labels/{}/{}", "well", "round", "label", "level"))
        .when(pl.col("kind") == "table").then(pl.coalesce(table_group, folder))
        .otherwise(folder),
        empty=pl.col("uncompressed") == 0,
        padding=((pl.col("kind") == "image") & (pl.col("uncompressed") == PADDING_FRAME["image"]))
        | ((pl.col("kind") == "label") & (pl.col("uncompressed") == PADDING_FRAME["label"])),
    )


def chunks_only(members: pl.DataFrame) -> pl.DataFrame:
    """The members that hold pixels."""
    return members.filter(pl.col("kind").is_in(["image", "label"]))


def kind_summary(members: pl.DataFrame) -> pl.DataFrame:
    """Members, compressed bytes, and empty and padding members, by kind."""
    return (
        members.group_by("kind")
        .agg(pl.len().alias("members"), pl.sum("compressed").alias("zip bytes"), pl.sum("empty"), pl.sum("padding"))
        .sort("members", descending=True)
    )


def wells_in_file_order(members: pl.DataFrame) -> pl.DataFrame:
    """Each well's region, member count and empty members, in file order."""
    return (
        members.filter(pl.col("well").is_not_null()).group_by("well")
        .agg(pl.min("start"), pl.max("end"), pl.len().alias("members"), pl.sum("empty"))
        .sort("start")
    )


def plate_members(members: pl.DataFrame) -> pl.DataFrame:
    """The members outside every well: the plate's and the rows' metadata."""
    return members.filter(pl.col("well").is_null())


def runs(members: pl.DataFrame) -> pl.DataFrame:
    """The zip's runs, in file order: consecutive members of one array or one metadata folder.

    One row per run with its ``array``, ``kind``, member count, span
    (``start``, ``end``), compressed bytes, and counts of empty and padding
    members.
    """
    starts_new_run = (pl.col("array") != pl.col("array").shift(1)).fill_null(True)
    return (
        members.with_columns(run=starts_new_run.cum_sum())
        .group_by("run", maintain_order=True)
        .agg(
            pl.first("array"), pl.first("kind"), pl.len().alias("members"),
            pl.first("start"), pl.last("end"),
            pl.sum("compressed"), pl.sum("empty"), pl.sum("padding"),
        )
    )


def chunk_order(chunks: pl.DataFrame) -> pl.DataFrame:
    """The (channel, z index) groups within one array's run, in file order.

    In S-BIAD3228 each channel's chunks are contiguous, and within a channel
    the z indices come in a fixed order that is neither numeric nor
    alphabetical.
    """
    group = pl.struct("c", "z")
    starts_new_group = (group != group.shift(1)).fill_null(True)
    return (
        chunks.with_columns(group_number=starts_new_group.cum_sum())
        .group_by("group_number", maintain_order=True)
        .agg(pl.first("c"), pl.first("z"), pl.len().alias("chunks"), pl.first("start"), pl.sum("compressed"))
    )


def highest_z_index(chunks: pl.DataFrame) -> int:
    """The largest z index among the chunks, the top of a z-index color scale."""
    return int(chunks["z"].max())


# ---------------------------------------------------------------------------
# A well's geometry, from its raw metadata
# ---------------------------------------------------------------------------

def well_geometry(raw: dict, well: str) -> dict:
    """What a well's raw metadata says about its pixels, fields and channels.

    Returns:

    - ``pixel_y_um``, ``pixel_x_um``: the level-0 pixel size in y and in x,
      from the ``multiscales`` scale of the round whose ``FOV_ROI_table`` was
      read (the first round).
    - ``grid``: each level's size in grid cells, as (rows, columns), from
      that round's ``.zarray`` shapes. Every round of a well has the same
      shapes.
    - ``fields``: each field of view's row of the ``FOV_ROI_table`` (name,
      corner and size in micrometers), sorted by field number.
    - ``channels``: each round's channel names, from ``omero``. Round 3's
      differ between wells.
    """
    documents = raw["documents"]
    rounds = sorted(name.split("/")[2] for name in documents if name.count("/") == 3 and name.endswith("/.zattrs"))
    reference = rounds[0]
    scale = documents[f"{well}/{reference}/.zattrs"]["multiscales"][0]["datasets"][0]["coordinateTransformations"][0]["scale"]
    grid = []
    for level in range(LEVELS):
        shape = documents[f"{well}/{reference}/{level}/.zarray"]["shape"]
        grid.append([math.ceil(shape[-2] / CHUNK_PIXELS), math.ceil(shape[-1] / CHUNK_PIXELS)])
    fields = sorted(raw["fov_table"], key=lambda row: int(row["name"].removeprefix("FOV_")))
    channels = {
        round_: [channel["label"] for channel in documents[f"{well}/{round_}/.zattrs"]["omero"]["channels"]]
        for round_ in rounds
    }
    return {"pixel_y_um": scale[-2], "pixel_x_um": scale[-1], "grid": grid, "fields": fields, "channels": channels}


def field_window(field: dict, level: int, geometry: dict) -> tuple[tuple[int, int], tuple[int, int]]:
    """A field's pixel window at ``level``, as ((row start, row end), (column start, column end)).

    The corner and size come from the field's row of the ``FOV_ROI_table``.
    At coarser levels the window covers every coarse pixel the field touches,
    so it can be one pixel wider than the field's size divided by 2**level.
    """
    row_start = round(field["y_micrometer"] / geometry["pixel_y_um"])
    column_start = round(field["x_micrometer"] / geometry["pixel_x_um"])
    row_end = row_start + round(field["len_y_micrometer"] / geometry["pixel_y_um"])
    column_end = column_start + round(field["len_x_micrometer"] / geometry["pixel_x_um"])
    scale = 2**level
    return (row_start // scale, math.ceil(row_end / scale)), (column_start // scale, math.ceil(column_end / scale))


def field_windows(geometry: dict) -> list[dict]:
    """Each field's name and its window at every level, as [row start, row end, column start, column end]."""
    windows = []
    for field in geometry["fields"]:
        by_level = [field_window(field, level, geometry) for level in range(LEVELS)]
        windows.append({"name": field["name"], "windows": [[*rows, *columns] for rows, columns in by_level]})
    return windows


def grid_cells(window: tuple[tuple[int, int], tuple[int, int]]) -> set[tuple[int, int]]:
    """The grid cells, as (row, column), that a pixel window overlaps."""
    (row_start, row_end), (column_start, column_end) = window
    return {
        (row, column)
        for row in range(row_start // CHUNK_PIXELS, (row_end - 1) // CHUNK_PIXELS + 1)
        for column in range(column_start // CHUNK_PIXELS, (column_end - 1) // CHUNK_PIXELS + 1)
    }


def touched_cells(geometry: dict) -> set[tuple[int, int, int]]:
    """Every (level, row, column) grid cell that some field of the well overlaps."""
    return {
        (level, row, column)
        for field in geometry["fields"]
        for level in range(LEVELS)
        for row, column in grid_cells(field_window(field, level, geometry))
    }


def fields_in_cell(geometry: dict, level: int, row: int, column: int) -> list[str]:
    """The names of the fields that overlap one grid cell."""
    return [
        field["name"] for field in geometry["fields"]
        if (row, column) in grid_cells(field_window(field, level, geometry))
    ]


def needed(chunks: pl.DataFrame, geometry: dict) -> np.ndarray:
    """For each chunk, whether a field overlaps its grid cell, so its pixels must be decoded."""
    touched = touched_cells(geometry)
    positions = zip(chunks["level"].to_list(), chunks["y"].to_list(), chunks["x"].to_list())
    return np.array([position in touched for position in positions], dtype=bool)


def chunk_flags(chunks: pl.DataFrame, geometry: dict) -> np.ndarray:
    """Each chunk's flags: :data:`FLAG_EMPTY`, :data:`FLAG_PADDING`, and :data:`FLAG_TO_DECODE` for a chunk to decode."""
    empty = chunks["empty"].to_numpy()
    padding = chunks["padding"].to_numpy()
    to_decode = needed(chunks, geometry) & ~padding & ~empty
    return (empty * FLAG_EMPTY | padding * FLAG_PADDING | to_decode * FLAG_TO_DECODE).astype(np.uint8)


# ---------------------------------------------------------------------------
# Questions the command line asks
# ---------------------------------------------------------------------------

def member_context(well_members: pl.DataFrame, member: dict, geometry: dict, range_bytes: int = 64 << 20) -> dict:
    """Where a chunk sits in its well's region, which planes it covers, and which fields need its grid cell."""
    into_region = member["start"] - int(well_members["start"].min())
    first_plane = member["z"] * PLANES_PER_CHUNK
    return {
        "into_region": into_region,
        "range_number": into_region // range_bytes,
        "planes": (first_plane, first_plane + PLANES_PER_CHUNK - 1),
        "fields": fields_in_cell(geometry, member["level"], member["y"], member["x"]),
    }


def merge_spans(members: pl.DataFrame, gap: int = 0) -> list[tuple[int, int]]:
    """The members' spans, merged where they touch or are at most ``gap`` bytes apart."""
    merged: list[list[int]] = []
    for start, end in zip(members["start"].to_list(), members["end"].to_list()):
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def field_chunks(well_members: pl.DataFrame, geometry: dict, field: dict, level: int) -> tuple[tuple, set, list[dict]]:
    """A field's window and grid cells at ``level``, and for each array the chunks the field needs.

    Each array's row gives ``array``, ``chunks``, ``compressed`` and
    ``spans``: how many separate spans those chunks make, which is how many
    requests reading them alone would take.
    """
    window = field_window(field, level, geometry)
    cells = grid_cells(window)
    chunks = chunks_only(well_members).filter(pl.col("level") == level)
    in_cells = pl.Series([(row, column) in cells for row, column in zip(chunks["y"].to_list(), chunks["x"].to_list())])
    arrays = []
    for (array,), array_chunks in chunks.filter(in_cells).group_by("array", maintain_order=True):
        arrays.append({"array": array, "chunks": len(array_chunks), "compressed": int(array_chunks["compressed"].sum()),
                       "spans": len(merge_spans(array_chunks.sort("start")))})
    return window, cells, arrays


def range_count(spans: list[tuple[int, int]], range_bytes: int) -> int:
    """How many range requests of at most ``range_bytes`` cover the spans."""
    return sum(math.ceil((end - start) / range_bytes) for start, end in spans)


def range_plan(well_members: pl.DataFrame, geometry: dict, range_bytes: int, gap_bytes: int) -> dict:
    """The requests to read a well: its whole region, or only the chunks that must be decoded.

    Returns the region's size and request count; the chunks to decode and
    their compressed bytes; those chunks' spans merged across gaps up to
    ``gap_bytes``, their bytes and the requests they take; and for each of
    :data:`READ_SETTINGS` the waves of requests, the seconds spent waiting
    and the seconds of transfer.
    """
    region = int(well_members["end"].max() - well_members["start"].min())
    chunks = chunks_only(well_members)
    to_decode = chunks.filter(pl.Series(needed(chunks, geometry)) & ~pl.col("padding") & ~pl.col("empty"))
    spans = merge_spans(to_decode, gap=gap_bytes)
    span_bytes = sum(end - start for start, end in spans)
    requests = range_count(spans, range_bytes)
    estimates = []
    for setting in READ_SETTINGS:
        waves = math.ceil(requests / setting.concurrency)
        estimates.append({"setting": setting, "waves": waves, "waiting_s": waves * setting.latency_s,
                          "transfer_s": span_bytes / setting.throughput})
    return {
        "region": region, "region_requests": math.ceil(region / range_bytes),
        "to_decode": len(to_decode), "to_decode_bytes": int(to_decode["compressed"].sum()),
        "spans": len(spans), "span_bytes": span_bytes, "requests": requests, "estimates": estimates,
    }


def directory_by_well(members: pl.DataFrame) -> list[dict]:
    """Which part of the central directory describes which well, in directory order.

    The directory lists members in file order, so each well's entries are
    contiguous too. Members outside every well count as ``plate``.
    """
    entries = members.sort("entry_offset").with_columns(pl.col("well").fill_null("plate"))
    starts_new_well = (pl.col("well") != pl.col("well").shift(1)).fill_null(True)
    parts = (
        entries.with_columns(part=starts_new_well.cum_sum())
        .group_by("part", maintain_order=True)
        .agg(pl.first("well"), pl.first("entry_offset").alias("start"),
             (pl.last("entry_offset") + pl.last("entry_bytes")).alias("end"), pl.len().alias("entries"))
    )
    return parts.select("well", "start", "end", "entries").to_dicts()
