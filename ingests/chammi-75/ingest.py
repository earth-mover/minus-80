"""Reference the permissively licensed studies of the CHAMMI-75 training set as virtual OME-Zarr.

CHAMMI-75 pools 2,792,462 multi-channel microscopy images from 74 public
studies, published in ``s3://chammi-data/CHAMMI-75/`` as one 8-bit grayscale
PNG per channel, a metadata CSV with one row per PNG, and a zip of cell
positions. The studies keep their own licenses. This ingest takes the 56
whose license is CC BY 4.0 or CC0 1.0 and builds one Icechunk store whose
pixels are references to the PNGs, never copies:

- ``<study>/``: one study, with its license;
- ``<study>/<stack>/``: the study's images that share an image size and a set
  of channels, named ``<width>x<height>_ch<slots>``;
- ``<study>/<stack>/ch<slot>/0``: one channel of every image in the stack,
  an OME-Zarr image ``(image, y, x)`` whose chunks are the PNGs themselves;
- ``index/<column>``: one row per image across every stack, with its stack,
  its row in the stack and every annotation the metadata states;
- ``cells/``: the cell positions the maintainers' training crops around, on
  the index's image axis.

The sections below run in pipeline order. PLAN.md gives the choices behind
the layout, and LAYOUTS.md how the bucket maps onto it.

    uv run --package chammi-75-ingest python ingests/chammi-75/ingest.py \\
        --repo <icechunk dir> [--studies nidr0013,nidr0014] [--work <dir>]

A run writes in three steps, so that studies can be written at once by
separate processes and land in one commit (docs/ingest.md, "Commit often,
and merge parallel writers once"):

1. The root group and its ``OME`` group are written and committed.
2. One session per study is forked from that commit, and each study is
   read from the bucket and written into its own fork, below its own path.
3. The forks are merged into one session. The tables on the image axis
   (``index/``, ``cells/``) and the collection's list of images are written
   into it from every study's rows, and the whole is committed once.

A study that fails stops the run before step 3, and nothing is committed
beyond step 1: an index that left out a study the store held, or held a
study the store left out, would mislead every reader.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import zarr
from safetensors.numpy import load as load_safetensors
from virtualizarr.manifests import ManifestGroup

from minus80.core.fetch import cache_dir
from minus80.core.icechunk_io import commit
from minus80.core.manifest import manifest_array
from minus80.ome import channel_omero, image_annotation, image_attrs, root_attrs, series_attrs

import pipeline
from pipeline import say

# The license allowlist
#
# CHAMMI-75 pools studies under their own licenses, listed in Table A4 of the
# paper (arXiv:2512.20833v2). The store holds only studies whose license lets
# anyone reuse the images, commercially included, without passing on a
# share-alike term: CC BY 4.0 and CC0 1.0. Every study the metadata names must
# be in exactly one of the two tables below, so a study added to a later
# release stops the run until someone reads its license.

PAPER = ("Agrawal et al., CHAMMI-75: Pre-training multi-channel models with heterogeneous "
         "microscopy images, arXiv:2512.20833v2 (2026)")
LICENSE_SOURCE = "arXiv:2512.20833v2, Table A4"
CC_BY = "CC-BY-4.0"
CC0 = "CC0-1.0"

#: The studies the store holds, with each one's license as Table A4 states it.
LICENSES = {
    **{s: CC_BY for s in (
        "idr0001 idr0002 idr0009 idr0010 idr0011 idr0022 idr0028 idr0030 idr0033 idr0035 "
        "idr0037 idr0056 idr0072 idr0081 idr0086 idr0089 idr0093 idr0115 idr0120 idr0123 "
        "idr0128 idr0129 idr0130 idr0133 idr0140 idr0145 nidr0001 nidr0002 nidr0004 "
        "nidr0005 nidr0006 nidr0008 nidr0009 nidr0010 nidr0012 nidr0013 nidr0014 nidr0015 "
        "nidr0016 nidr0017 nidr0019 nidr0020 nidr0025 nidr0027 nidr0028 nidr0029 nidr0030"
    ).split()},
    **{s: CC0 for s in (
        "idr0013 idr0080 idr0094 jump0001 nidr0003 nidr0007 nidr0011 nidr0023 nidr0024"
    ).split()},
}

SHARE_ALIKE = "share-alike (CC BY-SA)"
NOT_PERMISSIVE = "non-commercial or custom terms"
#: The studies the store leaves out, with the reason.
EXCLUDED = {
    **{s: SHARE_ALIKE for s in "hpa0018 hpa0023 idr0025".split()},
    **{s: NOT_PERMISSIVE for s in (
        "idr0003 idr0005 idr0006 idr0007 idr0008 idr0012 idr0017 idr0069 idr0088 "
        "nidr0018 nidr0021 nidr0022 nidr0031 nidr0032 wtc0001"
    ).split()},
}


def check_studies(named: set[str], requested: list[str] | None) -> list[str]:
    """The studies to ingest: ``requested``, or every allowlisted study.

    Raises if the metadata names a study in neither table, if ``requested``
    names a study that is not allowlisted, or if a study to ingest is missing
    from the metadata.
    """
    if unknown := sorted(named - LICENSES.keys() - EXCLUDED.keys()):
        raise ValueError(f"the metadata names studies with no recorded license decision: {unknown}; "
                         f"read their license and add each to LICENSES or EXCLUDED")
    if refused := sorted(set(requested or []) - LICENSES.keys()):
        reasons = {s: EXCLUDED.get(s, "no recorded license decision") for s in refused}
        raise ValueError(f"not allowlisted: {reasons}")
    studies = sorted(set(requested) if requested else LICENSES)
    if absent := sorted(set(studies) - named):
        raise ValueError(f"studies to ingest that the metadata does not name: {absent}")
    return studies


# What the store says about itself
#
# The root carries the dataset's description, the license of every study the
# release holds and whether the store took it, and every decision the data
# does not settle on its own. A reader with only the store can tell what it
# is, where its bytes live and what it may do with them.

#: Index columns taken from the metadata, by their name in the store. Each is
#: constant within an image except ``fov``, whose spellings differ (``9`` and
#: ``9.0``) and is normalized. Values stay text, as the source states them.
SOURCE_COLUMNS = {
    "image_id": "imaging.multi_channel_id",
    "study": "experiment.study",
    "study_type": "experiment.study_type",
    "plate": "experiment.plate",
    "well": "experiment.well",
    "control": "experiment.control",
    "reagent": "experiment.reagent",
    "organism": "biology.organism",
    "cell_line": "biology.cell_line",
    "cell_type": "biology.cell_type",
    "panel": "imaging.panel",
    "microscopy": "microscopy.type",
    "magnification": "microscopy.magnification",
    "fov": "microscopy.fov",
    "z_slice": "geometry.z_slice",
    "timepoint": "geometry.timepoint",
    "depth": "geometry.depth",
    "stated_width": "geometry.width",
    "stated_height": "geometry.height",
    "stated_channels": "geometry.channels",
}

DESCRIPTIONS = {
    **{k: f"{v}, as the source states it" for k, v in SOURCE_COLUMNS.items()},
    "image_id": "imaging.multi_channel_id: the image's identifier in the source",
    "fov": "microscopy.fov, with numbers normalized so that 9 and 9.0 are one value",
    "stack": "the stack holding the image, as a path from the root",
    "row": "the image's position along its stack's image axis",
    "license": f"the study's license, from {LICENSE_SOURCE}",
    "width": "image width in pixels, from the PNG headers",
    "height": "image height in pixels, from the PNG headers",
    "n_channels": "number of channel files the image has",
    "channels": "the image's panel slots (imaging.channel), in stack order, joined by '-'; "
                "slot s is the stack's ch<s> image",
    "channel_types": "imaging.channel_type of each slot, in stack order, joined by ','",
    "in_small": "whether the image is in the maintainers' small subset (CHAMMI-75_small_metadata.csv)",
    "has_positions": "whether CHAMMI-75_guidance.zip has a position file for the image",
    "segmentation": "how the maintainers found the image's cells: cellpose, threshold (edge "
                    "detection) or snake (worm watershed), per their config.tsv; empty where "
                    "there are no positions",
    "has_areas": "whether the image's position file has a sizes tensor, giving cells/area; "
                 "where it is false, the image's cells/area is 0",
    "background_dropped": "whether the first entry of the image's position file, the "
                          "background, was moved to cells/background (every Cellpose file)",
    "n_cells": "number of cell positions stored for the image",
}

INDEX_COLUMNS = ["stack", "row", *SOURCE_COLUMNS, "license", "width", "height", "n_channels",
                 "channels", "channel_types", "in_small", "has_positions", "segmentation",
                 "has_areas", "background_dropped", "n_cells"]
NUMERIC_COLUMNS = {"row", "width", "height", "n_channels", "n_cells", "in_small",
                   "has_positions", "has_areas", "background_dropped"}

#: The axes of every channel image. ``image`` is a custom axis: the images of
#: a stack were taken separately and share only their size and channels.
DIMS = ("image", "y", "x")
DIMENSIONS = {"image": {"type": "image"}, "y": {"type": "space"}, "x": {"type": "space"}}

ROOT = {
    "title": "CHAMMI-75 training set, CC BY 4.0 and CC0 studies",
    "description": (
        "The studies of the CHAMMI-75 training set whose license is CC BY 4.0 or CC0 1.0. "
        "CHAMMI-75 pools multi-channel microscopy images from public studies, gathered by the "
        "Caicedo lab (Morgridge Institute for Research) to pre-train models that accept any "
        "number of channels. Each channel is an 8-bit grayscale PNG in "
        "s3://chammi-data/CHAMMI-75/CHAMMI-75_train/, referenced in place: one PNG is one chunk. "
        "<study>/<stack> holds a study's images of one size and channel set; "
        "<study>/<stack>/ch<slot> is an OME-Zarr image (image, y, x) of one channel slot. "
        "index/ has one row per image with its stack and row and every annotation: query it to "
        "find images, then read <stack>/ch<slot>/0[row] for each slot in index/channels. "
        "cells/ holds the cell positions the maintainers' training crops around, on the same "
        "image axis as index/: index row i's cells are "
        "cells/position[image_start[i]:image_start[i + 1]]."),
    "citation": PAPER,
    "license": (f"per study, {CC_BY} or {CC0}: see licenses. Studies under other licenses "
                f"are in the release but not in this store."),
    "licenses": {
        "source": LICENSE_SOURCE,
        "included": LICENSES,
        "excluded": EXCLUDED,
    },
    "source": {
        "data": "s3://chammi-data/CHAMMI-75/ (us-west-2, anonymous)",
        "registry": "https://registry.opendata.aws/chammi/",
        "paper": "https://arxiv.org/abs/2512.20833v2",
        "code": "https://github.com/CaicedoLab/CHAMMI-75 (read at b84a4ec)",
        "documentation": "https://morgridge.org/research/labs/caicedo/chammi-75/",
        "metadata": f"{pipeline.METADATA_CSV}, {pipeline.SMALL_CSV}",
        "cell_positions": pipeline.GUIDANCE_ZIP,
    },
    "requires": {
        "codecs": "The pixel chunks use the imagecodecs_png codec: pip install imagecodecs-zarr. "
                  "The index and cells use standard Zarr codecs.",
        "virtual_chunks": "Pixel chunks are read from s3://chammi-data/ anonymously; authorize "
                          "that container when opening the repo.",
    },
    "decisions": {
        "licenses": (f"Only studies whose license is {CC_BY} or {CC0} in {LICENSE_SOURCE} are "
                     f"stored; licenses.excluded lists the rest and why. The index and cells hold "
                     f"no rows from an excluded study."),
        "channels_as_images": (
            "OME-Zarr allows one channel or custom-typed axis per image, so a stack's images, "
            "which need an image axis, are stored one channel slot per OME-Zarr image, "
            "<stack>/ch<slot>, each (image, y, x). Every channel image of a stack shares the "
            "image axis: row r of each is the same image."),
        "background_entry": (
            "Every Cellpose position file in CHAMMI-75_guidance.zip starts with the background "
            "(label 0) and lacks its last cell, from an off-by-one in the maintainers' "
            "utils/segmentation/segment.py (L261-273 at b84a4ec). The first entry is moved from "
            "every file of an image group that the maintainers' config.tsv routes to Cellpose, "
            "whether or not the file has a sizes tensor, to cells/background; "
            "index/segmentation and background_dropped say which. Files from the threshold and "
            "snake methods are kept whole. An image's cells/background followed by its run of "
            "cells/position is what the source file holds. The missing last cell cannot be "
            "recovered."),
        "image_size": ("Stack shapes come from the PNG headers. The metadata states the wrong "
                       "size for some files; index/stated_width and stated_height keep what it "
                       "states."),
        "channels": ("Channels are the files an image has, in panel-slot order. "
                     "geometry.channels is wrong for some images; index/stated_channels keeps "
                     "what it states."),
        "stacks": ("A stack holds a study's images that share a size and the same channel "
                   "type in each slot. Where two stacks of a study would share a name, the "
                   "channel types are appended to it."),
        "small_subset": (f"{pipeline.SMALL_CSV}'s paths start CHAMMI-75_small/, which is not in "
                         f"the bucket; the subset is matched by image ID into index/in_small."),
        "fov": ("microscopy.fov is written as 9 in some files of an image and 9.0 in others; "
                "the index keeps one value per image, with numbers normalized."),
        "positions_file": ("An image's positions are read from its lowest channel's file in the "
                           "zip; the files of an image's channels hold identical positions."),
        "display_colors": ("The source states no display colors. The omero colors are chosen "
                           "by the ingest: nuclei blue, transmitted light and EM gray, other "
                           "channels in a fixed order within a stack."),
    },
    "not_recorded": {
        "pixel_size": ("The source gives magnification only (index/magnification), so the y "
                       "and x scales are 1.0 with no unit."),
    },
}


# The metadata
#
# The metadata CSV has one row per PNG: the image it belongs to
# (imaging.multi_channel_id), its channel slot and type, and the image's
# annotations repeated on every channel's row. It is converted to Parquet
# once (pipeline.metadata_parquet) and each study reads its own rows.

def normalized(column: str) -> str:
    """SQL for a text column with its numbers written one way: 9.0 becomes 9."""
    number = f'try_cast("{column}" as double)'
    return (f'case when {number} is null then "{column}" '
            f"when {number} = floor({number}) then cast(cast({number} as bigint) as varchar) "
            f"else cast({number} as varchar) end")


def metadata_studies(metadata: Path) -> set[str]:
    """Every study the metadata names."""
    return set(study_sizes(metadata))


def study_sizes(metadata: Path) -> dict[str, int]:
    """Each study the metadata names, with its number of PNGs."""
    rows = duckdb.sql(f'select "experiment.study", count(*) from read_parquet(\'{metadata}\') group by 1').fetchall()
    return dict(rows)


# One study's images
#
# A study's PNGs are listed and the first 33 bytes of each read: the IHDR
# chunk states its width, height, bit depth and color type. The image size
# comes from there, not from the metadata, which states the wrong size for
# 397,130 files across the release. Each image's channel files are joined to
# the metadata, checked, and grouped into stacks of one size and channel set.

def png_header(header: bytes) -> tuple[int, int, int, int]:
    """(width, height, bit depth, color type) from a PNG's first 33 bytes."""
    if not header.startswith(pipeline.PNG_SIGNATURE) or header[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    return width, height, header[24], header[25]


def study_images(study: str, metadata: Path, small: Path, pngs: pa.Table) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The study's images, one row each with its stack and row, and its files, one row per PNG.

    ``pngs`` holds every PNG listed under the study, with ``key``, ``size``,
    ``width``, ``height``, ``bit_depth`` and ``color_type``. Raises unless
    every metadata row has a PNG and every PNG a metadata row, every PNG is
    8-bit grayscale, and within an image the size is one, the slots are
    distinct and every annotation except the channel's own is constant.
    """
    con = duckdb.connect()
    con.sql("set enable_progress_bar=false")
    con.register("pngs", pngs)
    con.sql(f"""create table f as
        select m.*, p.size, p.width, p.height, p.bit_depth, p.color_type,
               cast(cast(m."imaging.channel" as double) as int) slot,
               {normalized("microscopy.fov")} fov_n
        from read_parquet('{metadata}') m left join pngs p on p.key = m."storage.path"
        where m."experiment.study" = '{study}'""")
    if missing := con.sql("select count(*) from f where size is null").fetchall()[0][0]:
        raise ValueError(f"{study}: {missing} metadata rows name no PNG in the bucket")
    unlisted = con.sql("""select count(*) from pngs where key not in
                          (select "storage.path" from f)""").fetchall()[0][0]
    if unlisted:
        raise ValueError(f"{study}: {unlisted} PNGs in the bucket have no metadata row")
    if odd := con.sql("select count(*) from f where bit_depth <> 8 or color_type <> 0").fetchall()[0][0]:
        raise ValueError(f"{study}: {odd} PNGs are not 8-bit grayscale")

    constant = [c for c in SOURCE_COLUMNS.values()
                if c not in ("microscopy.fov", "imaging.multi_channel_id")]
    checks = ", ".join(f'count(distinct "{c}") > 1' for c in constant)
    varying = con.sql(f"""select count(*) from (
        select greatest({checks}, count(distinct fov_n) > 1, count(distinct width) > 1,
                        count(distinct height) > 1, count(distinct slot) <> count(*)) bad
        from f group by "imaging.multi_channel_id") where bad""").fetchall()[0][0]
    if varying:
        raise ValueError(f"{study}: {varying} images vary in a column the layout takes as per-image")

    picks = ", ".join(f'any_value("{c}") "{c}"' for c in constant)
    con.sql(f"""create table i as
        select "imaging.multi_channel_id" image_id, {picks}, any_value(fov_n) fov_n,
               any_value(width) width, any_value(height) height, count(*) n_channels,
               string_agg(slot::varchar, '-' order by slot) channels,
               string_agg("imaging.channel_type", ',' order by slot) channel_types,
               string_agg("imaging.channel_type", ',' order by "imaging.channel_type") type_set
        from f group by 1""")
    con.sql(f"""create table s as select distinct "imaging.multi_channel_id" image_id
                from read_parquet('{small}') where "experiment.study" = '{study}'""")

    # A stack is a size and the channel type in each slot. Slots alone are
    # not enough: in wtc0001 at 900x600 one panel string puts bright-field in
    # slot 1 for some images and membrane for others. Where two stacks would
    # share the short name, the channel types tell them apart.
    con.sql("""create table k as
        select *, width || 'x' || height || '_ch' || channels base from (
          select distinct "experiment.study" study, width, height, channels, channel_types from i)""")
    con.sql("""create table stacks as
        select *, study || '/' || case when count(*) over (partition by base) > 1
          then base || '_' || replace(replace(channel_types, ' ', '-'), ',', '_') else base end stack
        from k""")
    if not con.sql("select count(*) = count(distinct stack) from stacks").fetchall()[0][0]:
        raise ValueError(f"{study}: two stacks share a name")

    columns = ", ".join(f'i."{src}" "{name}"' for name, src in SOURCE_COLUMNS.items()
                        if src not in ("microscopy.fov", "imaging.multi_channel_id"))
    images = con.sql(f"""
        select k.stack, row_number() over (partition by k.stack order by i.image_id) - 1 "row",
               i.image_id, {columns}, i.fov_n fov, '{LICENSES[study]}' license,
               i.width, i.height, i.n_channels::int8 n_channels, i.channels, i.channel_types,
               i.type_set, s.image_id is not null in_small
        from i join stacks k on k.width = i.width and k.height = i.height
             and k.channels = i.channels and k.channel_types = i.channel_types
        left join s using (image_id)
        order by k.stack, "row" """).df()
    files = con.sql("""select "imaging.multi_channel_id" image_id, slot, "storage.path" "key", size
                       from f order by 1, 2""").df()
    order = images["image_id"].to_numpy()
    files = files.set_index("image_id").loc[order].reset_index()
    return images, files


# One study's cells
#
# CHAMMI-75_guidance.zip holds one safetensors file per PNG: ``data``, an
# int64 (n, 2) table of cell positions (x, y), and for most Cellpose files
# ``sizes``, each cell's area. An image's channel files hold identical
# positions, so each image's are read from its lowest channel's file.
#
# Cellpose files carry a bug: the maintainers' conversion loop runs over
# labels 0 to N-1 of a mask, so entry 0 is the background and the last cell is
# lost. Entry 0 is moved to cells/background for every image whose group the
# maintainers' config.tsv routes to Cellpose; the threshold and snake files
# are kept whole.

#: The image groups the maintainers segmented without Cellpose, by study and
#: channel types sorted by name: utils/segmentation/config.tsv at b84a4ec
#: routes these to "threshold" and "snake" and every other group with
#: positions to Cellpose. In Cellpose-routed groups, entry 0 of a file
#: without sizes lies within 5% of the image center in 78-100% of images per
#: group; in these four groups in 9-22%.
NOT_CELLPOSE = {
    ("idr0089", "RNA,nucleus,sub-nuclear"): "threshold",
    ("idr0089", "nucleus,protein,sub-nuclear"): "threshold",
    ("idr0089", "nucleus,sub-nuclear,sub-nuclear"): "threshold",
    ("nidr0007", "bright-field"): "snake",
}
#: cells/background where no entry was moved: every stored position is below 2,081.
NO_BACKGROUND = np.iinfo(np.uint16).max


def guidance_name(png_key: str) -> str:
    """The guidance zip's member for a PNG: the same path under ``CHAMMI-75_guidance/``, as safetensors."""
    relative = png_key.removeprefix(pipeline.TRAIN_PREFIX).removesuffix(".png")
    return f"{pipeline.GUIDANCE_PREFIX}{relative}.safetensors"


def fits(values: np.ndarray, dtype) -> bool:
    return values.size == 0 or (values.min() >= 0 and values.max() <= np.iinfo(dtype).max)


def study_cells(study: str, images: pd.DataFrame, files: pd.DataFrame, archive: str,
                members: Path) -> dict[str, np.ndarray]:
    """Each image's cells, in index order, and the per-image columns that describe them.

    Adds ``has_positions``, ``segmentation``, ``has_areas``,
    ``background_dropped`` and ``n_cells`` to ``images`` in place. Returns
    ``position`` (cell, 2) and ``area`` (cell,) for all the study's cells in
    image order, and ``background`` (image, 2).
    """
    lowest = files.groupby("image_id", sort=False)["key"].first()
    chosen = {guidance_name(k): image for image, k in lowest.items()}
    listed = pipeline.study_members(members, study)
    present = {m["name"] for m in listed} & chosen.keys()
    parsed = {}
    for name, contents in pipeline.read_members(archive, listed, present):
        tensors = load_safetensors(contents)
        for tensor, array in tensors.items():
            if array.dtype != np.int64:
                raise ValueError(f"{name}: tensor {tensor} is {array.dtype}, expected int64")
        parsed[chosen[name]] = (tensors["data"].reshape(-1, 2), tensors.get("sizes"))

    positions, areas = [], []
    background = np.full((len(images), 2), NO_BACKGROUND, dtype=np.uint16)
    columns = {k: [] for k in ("has_positions", "segmentation", "has_areas", "background_dropped", "n_cells")}
    for i, (image, type_set) in enumerate(zip(images["image_id"], images["type_set"])):
        if image not in parsed:
            for k, v in zip(columns, (False, "", False, False, 0)):
                columns[k].append(v)
            continue
        xy, sizes = parsed[image]
        method = NOT_CELLPOSE.get((study, type_set), "cellpose")
        if sizes is not None and method != "cellpose":
            raise ValueError(f"{image}: a file with sizes in a group routed to {method}")
        if method == "cellpose":
            if len(xy) == 0:
                raise ValueError(f"{image}: a Cellpose file with no background entry")
            background[i] = xy[0]
            xy = xy[1:]
            sizes = sizes[1:] if sizes is not None else None
        area = sizes if sizes is not None else np.zeros(len(xy), dtype=np.int64)
        if not (fits(xy, np.uint16) and fits(area, np.uint32)):
            raise ValueError(f"{image}: a position or area does not fit the stored types")
        positions.append(xy.astype(np.uint16))
        areas.append(area.astype(np.uint32))
        for k, v in zip(columns, (True, method, sizes is not None, method == "cellpose", len(xy))):
            columns[k].append(v)
    for k, v in columns.items():
        images[k] = v
    images["n_cells"] = images["n_cells"].astype(np.uint32)
    return {
        "position": np.concatenate(positions) if positions else np.empty((0, 2), np.uint16),
        "area": np.concatenate(areas) if areas else np.empty(0, np.uint32),
        "background": background,
    }


# One study's groups
#
# Each stack becomes a group holding one OME-Zarr image per channel slot.
# A channel image's chunk at row r is the PNG of image r for that slot,
# referenced whole: one chunk is one PNG, shape (1, height, width), decoded by
# the imagecodecs_png codec. Nothing is decoded to build it.

#: Display colors. The source states none; these are chosen here so that the
#: channel images of a stack composite legibly, and the root says so.
#: Transmitted-light and EM channels are gray, nuclei blue, and the rest take
#: the next free color in this order.
GRAY_TYPES = {"bright-field", "phase-contrast microscopy", "DIC microscopy",
              "dark-field microscopy", "electron microscopy"}
COLORS = ["00FF00", "FF0000", "FF00FF", "00FFFF", "FFFF00", "FF8000", "8000FF"]


def channel_colors(types: list[str]) -> list[str]:
    """The display color of each slot of a stack; see :data:`COLORS`."""
    free = iter(COLORS)
    return ["0000FF" if t == "nucleus" else "FFFFFF" if t in GRAY_TYPES else next(free) for t in types]


def stack_group(archive: str, stack: str, rows: pd.DataFrame, keys: np.ndarray, sizes: np.ndarray) -> ManifestGroup:
    """One stack: a ``ch<slot>`` OME-Zarr image per channel slot, referencing the PNGs."""
    first = rows.iloc[0]
    n, height, width = len(rows), int(first["height"]), int(first["width"])
    slots = [int(s) for s in first["channels"].split("-")]
    types = first["channel_types"].split(",")
    study = first["study"]
    urls = np.array([pipeline.png_url(archive, k) for k in keys], dtype=object).reshape(n, len(slots))
    lengths = sizes.astype(np.uint64).reshape(n, len(slots))
    channels = {}
    for j, (slot, kind, color) in enumerate(zip(slots, types, channel_colors(types))):
        name = f"ch{slot}"
        array = manifest_array(
            urls[:, j].reshape(n, 1, 1), np.zeros((n, 1, 1)), lengths[:, j].reshape(n, 1, 1),
            shape=(n, height, width), chunk_shape=(1, height, width), dtype="uint8",
            codecs=[{"name": "imagecodecs_png"}], dimension_names=DIMS,
            attributes={"dimensions": DIMENSIONS})
        ome = image_attrs(
            name=f"{stack}/{name}", dims=DIMS, level_shapes=[(n, height, width)],
            scale={d: 1.0 for d in DIMS}, dimensions=DIMENSIONS,
            description=(f"Channel slot {slot} ({kind}) of {n:,} images from {study}. Each "
                         f"(image) chunk is one referenced PNG; there are no lower resolution levels."),
            omero=channel_omero([{"label": f"ch{slot} {kind}", "color": color, "start": 0, "end": 255}],
                                max_value=255))
        channels[name] = ManifestGroup(arrays={"0": array}, attributes={"ome": ome} | image_annotation({
            "study": study, "license": LICENSES[study], "stack": stack, "slot": slot,
            "channel_type": kind, "images": n,
            "rows": f"row r is the image at /index where stack is {stack!r} and row is r"}))
    return ManifestGroup(groups=channels, attributes=image_annotation({
        "study": study, "license": LICENSES[study], "images": n, "width": width, "height": height,
        "channels": [{"slot": s, "type": t, "image": f"ch{s}"} for s, t in zip(slots, types)],
        "description": (f"{n:,} images from {study}, each {width}x{height} pixels with "
                        f"{len(slots)} channels, one OME-Zarr image per channel slot. Row r of "
                        f"every channel image is the same image. Its annotations and cells are in "
                        f"/index and /cells, at the rows where /index/stack is {stack!r}.")}))


def study_group(archive: str, study: str, images: pd.DataFrame, files: pd.DataFrame) -> ManifestGroup:
    """One study's group: its stacks, with the study's license."""
    keys, sizes = files["key"].to_numpy(), files["size"].to_numpy()
    ends = np.cumsum(images["n_channels"].to_numpy(np.int64))
    starts = ends - images["n_channels"].to_numpy(np.int64)
    stacks = {}
    for stack, rows in images.groupby("stack", sort=True):
        lo, hi = starts[rows.index[0]], ends[rows.index[-1]]
        stacks[stack.split("/", 1)[1]] = stack_group(archive, stack, rows, keys[lo:hi], sizes[lo:hi])
    return ManifestGroup(groups=stacks, attributes=image_annotation({
        "study": study, "study_type": images["study_type"].iloc[0],
        "license": LICENSES[study], "license_source": LICENSE_SOURCE,
        "images": len(images), "stacks": sorted(stacks),
        "description": f"The {len(images):,} images of {study} in CHAMMI-75's training set, by stack."}))


def study_job(fork, study: str, archive: str, work: Path, metadata: Path, small: Path, members: Path):
    """Read study ``study`` from the bucket and write its group into ``fork``.

    Returns the fork and a summary. The study's index rows and cells are
    saved under ``work`` for the step that writes the tables on the image
    axis, because the fork returns to the parent by pickling and the cells of
    a large study run to hundreds of megabytes.
    """
    started = time.monotonic()
    store = pipeline.archive_store(archive)
    listing = pipeline.list_pngs(store, study)
    headers = pipeline.read_png_headers(store, listing["key"].to_pylist())
    parsed = []
    for key, header in zip(listing["key"].to_pylist(), headers):
        try:
            parsed.append(png_header(header))
        except ValueError as error:
            raise ValueError(f"{key}: {error}") from None
    w, h, depth, color = (list(c) for c in zip(*parsed)) if parsed else ([],) * 4
    pngs = listing.append_column("width", pa.array(w, pa.int32())).append_column(
        "height", pa.array(h, pa.int32())).append_column(
        "bit_depth", pa.array(depth, pa.int16())).append_column("color_type", pa.array(color, pa.int16()))
    say(f"{study}: {pngs.num_rows:,} PNG headers read in {time.monotonic() - started:.0f} s")

    images, files = study_images(study, metadata, small, pngs)
    cells = study_cells(study, images, files, archive, members)
    say(f"{study}: {len(images):,} images, {images['stack'].nunique():,} stacks, "
        f"{len(cells['position']):,} cells")

    group = study_group(archive, study, images, files)
    group.to_icechunk(fork.store, group=study)
    out = work / "studies" / study
    out.mkdir(parents=True, exist_ok=True)
    images.drop(columns=["type_set"]).to_parquet(out / "images.parquet")
    np.savez(out / "cells.npz", **cells)
    return fork, {"study": study, "images": len(images), "pngs": len(files),
                  "stacks": images["stack"].nunique(), "cells": len(cells["position"]),
                  "seconds": round(time.monotonic() - started, 1)}


# The tables on the image axis
#
# Every image's annotations and cells sit at the root, on one image axis
# shared by every stack, rather than in each stack: Icechunk writes a manifest
# per array, and a copy of every column in each of 1,394 stacks made 44,600
# arrays whose commit ran for over 25 minutes. The pixels cannot share the
# axis, because a chunk is a whole PNG and an array has one chunk shape.

#: Cells per chunk of the cells/ arrays.
CELL_CHUNK = 65_536


def index_column(name: str, values) -> np.ndarray:
    if name in NUMERIC_COLUMNS:
        return np.asarray(values)
    return np.asarray(values).astype(str).astype(np.dtypes.StringDType())


def write_tables(root: zarr.Group, images: pd.DataFrame, cells: dict[str, np.ndarray]) -> None:
    """``index/`` and ``cells/`` for every image, in index order."""
    zstd = [zarr.codecs.ZstdCodec(level=5)]
    index = root.create_group("index", attributes={"index": {
        "description": ("One row per image across all stacks, sorted by stack then row. Find "
                        "images here, then read <stack>/ch<slot>/0[row] for each slot in "
                        "channels. Every array here and in /cells is aligned on this image axis."),
        "columns": {k: DESCRIPTIONS[k] for k in INDEX_COLUMNS}}})
    for name in INDEX_COLUMNS:
        data = index_column(name, images[name])
        index.create_array(name, data=data, chunks=(max(len(data), 1),), compressors=zstd,
                           dimension_names=("image",), attributes={"description": DESCRIPTIONS[name]})

    image_start = np.concatenate([[0], np.cumsum(images["n_cells"].to_numpy(np.int64))]).astype(np.uint64)
    blosc = [zarr.codecs.BloscCodec(cname="zstd", clevel=5, shuffle="shuffle")]
    group = root.create_group("cells", attributes={"cells": {
        "description": ("Cell positions from CHAMMI-75_guidance.zip on /index's image axis: index "
                        "row i's cells are rows image_start[i] to image_start[i + 1] of position "
                        "and area. index/segmentation says how each image's cells were found. "
                        "Cellpose positions have the source's first entry, the background, moved "
                        "to background, and lack the source's missing last cell (/ decisions)."),
        "segmentation": sorted(set(images.loc[images["has_positions"], "segmentation"]))}})
    group.create_array("image_start", data=image_start, chunks=(CELL_CHUNK,), compressors=blosc,
                       dimension_names=("image_edge",),
                       attributes={"description": "row of each index row's first cell; the last "
                                                  "entry is the number of cells"})
    group.create_array("position", data=cells["position"], chunks=(CELL_CHUNK, 2), compressors=blosc,
                       dimension_names=("cell", "xy"),
                       attributes={"description": "cell center (x, y) in pixels of the image", "unit": "pixel"})
    group.create_array("area", data=cells["area"], chunks=(CELL_CHUNK,), compressors=blosc,
                       dimension_names=("cell",),
                       attributes={"description": "cell area from Cellpose; 0 for the cells of images "
                                                  "whose file has no sizes (index/has_areas)",
                                   "unit": "pixel"})
    group.create_array("background", data=cells["background"], chunks=(CELL_CHUNK, 2), compressors=blosc,
                       fill_value=NO_BACKGROUND, dimension_names=("image", "xy"),
                       attributes={"description": ("the first entry of the image's position file, "
                                                   "the background (x, y), where it was moved "
                                                   f"(index/background_dropped); {NO_BACKGROUND} "
                                                   "elsewhere"),
                                   "unit": "pixel"})


def gather(work: Path, studies: list[str]) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    """Every study's index rows and cells, in index order."""
    images, cells = [], {"position": [], "area": [], "background": []}
    for study in studies:
        out = work / "studies" / study
        images.append(pd.read_parquet(out / "images.parquet"))
        with np.load(out / "cells.npz") as saved:
            for k in cells:
                cells[k].append(saved[k])
    table = pd.concat(images, ignore_index=True)
    ordered = table.sort_values(["stack", "row"], kind="stable").index
    if not np.array_equal(ordered, np.arange(len(table))):
        raise ValueError("the studies' rows are not in stack then row order")
    if leaked := sorted(set(table["study"]) - LICENSES.keys()):
        raise ValueError(f"rows from studies that are not allowlisted: {leaked}")
    return table, {k: np.concatenate(v) for k, v in cells.items()}


# The run

#: Seconds between the driver's progress lines while it waits on studies.
PROGRESS_EVERY = 30.0


def run(location: str, branch: str, archive: str, work: Path, requested: list[str] | None,
        parallel: int) -> str:
    """Ingest the studies into ``branch`` of the repo at ``location``; the snapshot committed."""
    started = time.monotonic()
    metadata = pipeline.metadata_parquet(archive, work, pipeline.METADATA_CSV)
    small = pipeline.metadata_parquet(archive, work, pipeline.SMALL_CSV)
    studies = check_studies(metadata_studies(metadata), requested)
    members = pipeline.guidance_members(archive, work)
    sizes = study_sizes(metadata)
    say(f"inputs ready in {time.monotonic() - started:.0f} s; {len(studies)} studies to write")

    repo = pipeline.open_for_writing(location, branch, archive)
    session = repo.writable_session(branch)
    root = zarr.open_group(session.store, mode="a", zarr_format=3)
    if dict(root.attrs).get("title") != ROOT["title"] or "OME" not in root:
        root.attrs.update({**root_attrs(), **ROOT})
        root.require_group("OME")
        say(f"root: committed {commit(session, 'CHAMMI-75: the root group and its OME group')}")

    session = repo.writable_session(branch)
    root = zarr.open_group(session.store, mode="r")
    if present := sorted(set(studies) & set(root.group_keys())):
        raise ValueError(f"already in the store: {present}; remove them first, or ingest the rest")
    forks = {study: session.fork() for study in studies}
    done, failed = {}, {}
    arguments = (archive, work, metadata, small, members)
    if parallel == 1:
        for study in studies:
            try:
                done[study] = study_job(forks[study], study, *arguments)
            except Exception as error:  # noqa: BLE001 -- reported below with the others
                failed[study] = f"{type(error).__name__}: {error}"
    else:
        # spawn, not fork: a forked child would inherit Icechunk's and
        # obstore's runtime threads mid-state.
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(parallel, mp_context=context) as pool:
            # Largest first, so the longest study does not start last and hold up the run.
            largest = sorted(studies, key=lambda s: -sizes[s])
            futures = {pool.submit(study_job, forks[s], s, *arguments): s for s in largest}
            pending = set(futures)
            while pending:
                finished, pending = wait(pending, timeout=PROGRESS_EVERY, return_when=FIRST_COMPLETED)
                for future in finished:
                    study = futures[future]
                    try:
                        done[study] = future.result()
                    except Exception as error:  # noqa: BLE001 -- reported below with the others
                        failed[study] = f"{type(error).__name__}: {error}"
                say(f"{len(done)} studies written, {len(failed)} failed, {len(pending)} running or waiting")
    for study, why in sorted(failed.items()):
        say(f"FAILED {study}: {why}")
    if failed:
        raise SystemExit(f"{len(failed)} studies failed; nothing committed beyond the root")
    for _, summary in sorted(done.values(), key=lambda d: d[1]["study"]):
        say(f"{summary['study']}: {summary['images']:,} images, {summary['pngs']:,} PNGs, "
            f"{summary['stacks']} stacks, {summary['cells']:,} cells in {summary['seconds']} s")

    merging = time.monotonic()
    session.merge(*(fork for fork, _ in done.values()))
    images, cells = gather(work, studies)
    root = zarr.open_group(session.store, mode="a", zarr_format=3)
    held = sorted(set(root.group_keys()) - {"OME", "index", "cells"})
    if held != studies:
        raise ValueError(f"the store holds studies {held}; this run wrote {studies}")
    series = [f"{stack}/ch{slot}" for stack, channels in
              images.drop_duplicates("stack")[["stack", "channels"]].itertuples(index=False)
              for slot in channels.split("-")]
    root["OME"].attrs.update(series_attrs(series))
    root.attrs.update({"contents": {"studies": len(studies), "stacks": int(images["stack"].nunique()),
                                    "channel_images": len(series), "images": len(images),
                                    "pngs": int(images["n_channels"].sum()),
                                    "cells": len(cells["position"])}})
    write_tables(root, images, cells)
    named = ", ".join(studies) if len(studies) <= 5 else "every allowlisted study"
    message = (f"CHAMMI-75: {len(studies)} studies ({named}), {len(images):,} images in "
               f"{images['stack'].nunique():,} stacks, with the index and cells")
    snapshot = commit(session, message)
    say(f"merged {len(done)} forks, wrote the index and cells, committed {snapshot} to {branch} in "
        f"{time.monotonic() - merging:.0f} s; whole run {time.monotonic() - started:.0f} s")
    return snapshot


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True,
                        help="Icechunk repo: a directory, an s3:// URL or arraylake://<org>/<repo>")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--studies", help="comma-separated allowlisted studies; default every one")
    parser.add_argument("--archive", default=pipeline.ARCHIVE, help="where CHAMMI-75 lives")
    parser.add_argument("--work", type=Path, default=Path(cache_dir()) / "chammi-75",
                        help="where the metadata, the zip's directory and each study's rows are kept")
    parser.add_argument("--parallel", type=int, default=8, help="studies written at once")
    args = parser.parse_args(argv)
    if args.archive.startswith("s3://"):
        # A fact of the bucket, not of the shell: without it, the repo's
        # container would sign requests with whatever credentials the build had.
        os.environ["MINUS80_S3_ANONYMOUS"] = "1"
    requested = args.studies.split(",") if args.studies else None
    run(args.repo, args.branch, args.archive, args.work, requested, args.parallel)


if __name__ == "__main__":
    sys.exit(main())
