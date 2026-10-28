"""What the S-BIAD3228 zip's layout is, computed from its central directory and a well's metadata.

Every function here is pure: it takes data frames and dicts that
:mod:`inputs` loaded and returns new ones, and fetches nothing. The terms are
the ones the tools and the ingest's PLAN.md use:

- **member**: one file inside the zip. Its name is a Zarr v2 key.
- **chunk**: a member that holds pixels, one source Zarr chunk: 1 channel x
  10 planes x 1000 x 1000 pixels for an image, 10 planes x 1000 x 1000 for a
  label. ``C/02/2/0/1/13/0/7`` is well ``C/02``, round 2, pyramid level 0,
  then the chunk's index on each axis of ``(c, z, y, x)``: channel 1, z index
  13 (planes 130 to 139), grid row 0 and column 7.
- **padding**: a chunk that is all zeros, recognizable from its size alone:
  an 80,897-byte Blosc frame for an image, 161,455 for a label. Four image
  chunks are 161,455 bytes and hold real pixels, so the size is checked
  against the chunk's kind.
- **empty**: a member with no bytes at all; it reads as 0.
- **run**: consecutive members of one array, or of one metadata folder. The
  zip stores each well's members contiguously and, within a well, one array
  after another.
- **range**: one range request for consecutive bytes of the zip, such as
  64 MiB of a well.
- **grid cell**: the (y, x) position of a chunk in its level's chunk grid.
  A field of view is 1000 x 1000 pixels placed anywhere on its well's image,
  so it overlaps one to four grid cells at each level.

A member's bytes in the zip run from its local header to the next member's
local header (``start`` to ``end``), so the members tile the file with
nothing between them.
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl

CHUNK_PIXELS = 1000
PLANES_PER_CHUNK = 10
LEVELS = 4
#: The Blosc frame size of an all-zero chunk, by kind.
PADDING_FRAME = {"image": 80_897, "label": 161_455}
IMAGE_CHUNK = r"^([B-G]/0[2-7])/(\d)/(\d)/(\d+)/(\d+)/(\d+)/(\d+)$"
LABEL_CHUNK = r"^([B-G]/0[2-7])/(\d)/labels/([^/]+)/(\d)/(\d+)/(\d+)/(\d+)$"


def describe_members(directory: pl.DataFrame, directory_offset: int) -> pl.DataFrame:
    """The central directory in file order, with each member's name split into what it addresses.

    Adds ``start`` and ``end`` (the member's bytes in the zip), ``kind``
    (``image``, ``label``, ``table`` or ``meta``), the well, round, label
    name, level and chunk index (``c``, ``z``, ``y``, ``x``) where the name
    has them, ``array`` (the array or folder the member belongs to), and the
    ``empty`` and ``padding`` flags.
    """
    name = pl.col("name")
    image = name.str.extract_groups(IMAGE_CHUNK)
    label = name.str.extract_groups(LABEL_CHUNK)
    members = directory.rename({"local_header_offset": "start"}).sort("start")
    members = members.with_columns(
        end=pl.col("start").shift(-1).fill_null(directory_offset),
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


def runs(members: pl.DataFrame) -> pl.DataFrame:
    """The zip's runs, in file order: consecutive members of one array or one metadata folder.

    One row per run with its ``array``, ``kind``, member count, byte span
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
    """The order of (channel, z index) groups within one array's run.

    One row per stretch of consecutive chunks that share a channel and z
    index, in file order. In S-BIAD3228 each channel's chunks are contiguous,
    and within a channel the z indices come in a fixed order that is neither
    numeric nor alphabetical.
    """
    group = pl.struct("c", "z")
    starts_new_group = (group != group.shift(1)).fill_null(True)
    return (
        chunks.with_columns(group_number=starts_new_group.cum_sum())
        .group_by("group_number", maintain_order=True)
        .agg(pl.first("c"), pl.first("z"), pl.len().alias("chunks"), pl.first("start"), pl.sum("compressed"))
    )


def field_window(field: dict, level: int, pixel_um: float) -> tuple[tuple[int, int], tuple[int, int]]:
    """A field's pixel window at ``level``, as ((y start, y end), (x start, x end)).

    The field's corner comes from its position in micrometers. At coarser
    levels the window covers every coarse pixel the field touches, so it can
    be one pixel wider than 1000 / 2**level.
    """
    corner_y = round(field["y_micrometer"] / pixel_um)
    corner_x = round(field["x_micrometer"] / pixel_um)
    scale = 2**level
    rows = (corner_y // scale, math.ceil((corner_y + CHUNK_PIXELS) / scale))
    columns = (corner_x // scale, math.ceil((corner_x + CHUNK_PIXELS) / scale))
    return rows, columns


def grid_cells(window: tuple[tuple[int, int], tuple[int, int]]) -> set[tuple[int, int]]:
    """The grid cells, as (row, column), that a pixel window overlaps."""
    (row_start, row_end), (column_start, column_end) = window
    return {
        (row, column)
        for row in range(row_start // CHUNK_PIXELS, (row_end - 1) // CHUNK_PIXELS + 1)
        for column in range(column_start // CHUNK_PIXELS, (column_end - 1) // CHUNK_PIXELS + 1)
    }


def touched_cells(metadata: dict) -> set[tuple[int, int, int]]:
    """Every (level, row, column) grid cell that some field of the well overlaps."""
    return {
        (level, row, column)
        for field in metadata["fields"]
        for level in range(LEVELS)
        for row, column in grid_cells(field_window(field, level, metadata["pixel_um"]))
    }


def fields_in_cell(metadata: dict, level: int, row: int, column: int) -> list[str]:
    """The names of the fields that overlap one grid cell."""
    return [
        field["name"] for field in metadata["fields"]
        if (row, column) in grid_cells(field_window(field, level, metadata["pixel_um"]))
    ]


def needed(chunks: pl.DataFrame, metadata: dict) -> np.ndarray:
    """For each chunk, whether a field overlaps its grid cell, so its pixels must be decoded."""
    touched = touched_cells(metadata)
    positions = zip(chunks["level"].to_list(), chunks["y"].to_list(), chunks["x"].to_list())
    return np.array([position in touched for position in positions], dtype=bool)


def merge_spans(members: pl.DataFrame, gap: int = 0) -> list[tuple[int, int]]:
    """The members' byte spans, merged where they touch or are at most ``gap`` bytes apart."""
    merged: list[list[int]] = []
    for start, end in zip(members["start"].to_list(), members["end"].to_list()):
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def range_count(spans: list[tuple[int, int]], range_bytes: int) -> int:
    """How many range requests of at most ``range_bytes`` cover the spans."""
    return sum(math.ceil((end - start) / range_bytes) for start, end in spans)


def directory_by_well(members: pl.DataFrame) -> list[dict]:
    """Which part of the central directory describes which well, in directory order.

    The directory lists members in file order, so each well's entries are
    contiguous too. Members outside every well count as ``plate``.
    """
    entries = members.sort("entry_offset").with_columns(pl.col("well").fill_null("plate"))
    starts_new_well = (pl.col("well") != pl.col("well").shift(1)).fill_null(True)
    spans = (
        entries.with_columns(span=starts_new_well.cum_sum())
        .group_by("span", maintain_order=True)
        .agg(pl.first("well"), pl.first("entry_offset").alias("start"),
             (pl.last("entry_offset") + pl.last("entry_bytes")).alias("end"), pl.len().alias("entries"))
    )
    return spans.select("well", "start", "end", "entries").to_dicts()
