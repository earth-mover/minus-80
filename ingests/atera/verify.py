"""Check an Atera repo and its Iceberg catalog, local or on Arraylake, against the bundle, gate by gate.

Each gate prints PASS, or FAIL and its mismatches. The script exits non-zero on any failure.
``--mode fixture`` checks the fixture branch: a gate skips an element that the branch lacks.
``--mode full`` checks every element of the full tree, whatever the branch, and fails on a missing one.
Each expectation comes from the bundle, except four. Gate `complete` uses the ingest's element list.
Gate `table_region` uses the ingest's region name. Gate `source` uses the record in LAYOUTS.md.
Gate `bundle` uses the sample's record of its bundle.
The script only reads.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import shapely
import zarr
from anndata.io import read_elem, sparse_dataset
from minus80.core.blocks import compare_blocks, sampled_blocks
from minus80.core.elements import COMPLETE
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, open_repo
from minus80.core.progress import MIB, Progress
from minus80.core.pyramid import check_page
from minus80.core.registry import as_url
from minus80.core.remote import reader
from minus80.core.tables import (
    COPY_CHUNK, copy_edges, local_catalog, parquet_field_ids, partition_bounds, partition_counts, partition_files,
    row_group_bounds,
)
from minus80.core.zarr_copy import create_copy
from minus80.core.zarr_copy import read_array
from pyiceberg.schema import index_by_name
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from bundle import CELLS, MATRIX, TRANSCRIPTS, he_readers, open_bundle, open_tiff
from check_bundle import check
from facts import load_record
from ingest import (
    CELL_SET, DIFFEXP_TABLES, ELEMENTS, EXTRA_TABLES, FIXTURE_TILE, ICEBERG_TABLES, INGEST_SKIPS, INSTANCE_KEY, LAYOUTS,
    MASKS, MISSING_FROM_FULL, NUCLEUS_SET, TABLE_REGION, catalog_org, require_same_place,
)
from qc import QC_NAMES, bundle_rasters, data_bag
from root_attrs import source_objects
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, S3, SAMPLES
from transcripts import tile_order, xenium_cell_id

SAMPLED = 50
MATRIX_CHUNKS = 20
#: Blocks between the first and the last that each copy compares with its data file.
COPY_SAMPLES = 3
IMAGE_TILES = 4
#: Half the side of the window that holds a sampled label, in pixels: 32 micrometres.
WINDOW = 150
SEED = 0
#: The relative error that a stored transform may have against its source number.
TRANSFORM_TOLERANCE = 1e-9
#: The spellings of the micrometre in OME-XML: the micro sign and the Greek letter.
MICROMETRE = ("µm", "\u03bcm")
#: The polygon set that each shapes element holds.
POLYGON_SETS = {"shapes/nucleus_boundaries": NUCLEUS_SET, "shapes/cell_boundaries": CELL_SET}


def raw(store, key: str) -> bytes | None:
    buffer = sync(store.get(key, default_buffer_prototype()))
    return None if buffer is None else buffer.to_bytes()


def chunk_members(zf, prefix: str) -> list[str]:
    """The chunk members of the v2 array at ``prefix``, whether its separator is ``.`` or ``/``."""
    stem = f"{prefix}/"
    return [n for n in zf.namelist() if n.startswith(stem) and not n.endswith("/")
            and n[len(stem):].replace(".", "").replace("/", "").isdigit()]


def chunk_key(array_path: str, prefix: str, member: str) -> str:
    """The key under the v3 array ``array_path`` of ``member``, a chunk of the v2 array at ``prefix``."""
    return f"{array_path}/c/" + member[len(prefix) + 1:].replace(".", "/")


def _name(location: str) -> str:
    """The file name of a data file's location; messages never print the storage prefix."""
    return location.rsplit("/", 1)[-1]


def full_elements() -> list[str]:
    """The elements of the full tree: those that the full mode of the ingest writes, but the root attributes."""
    return [e for e in ELEMENTS["full"] if e != "root"]


def _present(root, path: str, full: bool, errors: list[str]) -> bool:
    """True when the store has ``path``. A full store without it gets an error; a fixture skips it."""
    if path in root:
        return True
    if full:
        errors.append(f"{path}: missing")
    return False


def sampled_tiles(page) -> list[tuple[int, int]]:
    """``IMAGE_TILES`` tiles of the page's grid, spread over it; all of them when the grid is smaller."""
    grid = [(r, c) for r in range(math.ceil(page.imagelength / page.tilelength))
            for c in range(math.ceil(page.imagewidth / page.tilewidth))]
    return [grid[i] for i in np.linspace(0, len(grid) - 1, min(IMAGE_TILES, len(grid))).astype(int)]


def check_level(label: str, array, entries, progress: Progress) -> list[str]:
    """Compare the sampled tiles of each ``(name, page, index)`` of ``entries`` with ``array``; print one line."""
    errors = []
    for name, page, index in entries:
        tiles = sampled_tiles(page)
        errors += [f"{label} {name}".strip() + f": {e}" for e in check_page(array, page, index, tiles)]
        progress.advance()
    print(f"  images: {label}: {len(entries)} pages, {len(tiles)} tiles each")
    return errors


def stored_levels(group) -> list[int]:
    return sorted(int(name.removeprefix("s")) for name, _ in group.arrays())


def level_errors(root, path: str, count: int, full: bool = False) -> list[str]:
    """A group marked complete, or any group of a full store, holds every one of the source's ``count`` levels."""
    group = root[path]
    if stored_levels(group) == list(range(count)):
        return []
    if group.attrs.get(COMPLETE) is True:
        return [f"{path}: marked complete, holds levels {stored_levels(group)} of {count}"]
    return [f"{path}: holds levels {stored_levels(group)} of {count}"] if full else []


def gate_images(root, bundle, he_url: str, *, full: bool = False) -> list[str]:
    """Four sampled tiles of every stored level of each image element against tifffile's decode.

    The 3D stack is compared on every plane. ``he_url`` is read only when the store has the H&E element.
    A full store must hold each element with every level; a fixture skips an element that it lacks.
    """
    errors, work = [], []
    if _present(root, "images/morphology", full, errors):
        pyramids = [bundle.tiff(name).series[0].levels for name in bundle.morphology_2d()]
        errors += level_errors(root, "images/morphology", len(pyramids[0]), full)
        for level in stored_levels(root["images/morphology"]):
            entries = [(f"channel {c}", pyramid[level].pages[0], (c,)) for c, pyramid in enumerate(pyramids)]
            work.append((f"morphology s{level}", root[f"images/morphology/s{level}"], entries))
    if _present(root, "images/morphology_3d", full, errors):
        # One series holds every plane: its level k has one page for each plane.
        pyramid = bundle.tiff(bundle.morphology_3d()).series[0].levels
        errors += level_errors(root, "images/morphology_3d", len(pyramid), full)
        for level in stored_levels(root["images/morphology_3d"]):
            entries = [(f"plane {z}", page, (0, z)) for z, page in enumerate(pyramid[level].pages)]
            work.append((f"morphology_3d s{level}", root[f"images/morphology_3d/s{level}"], entries))
    if _present(root, "images/he_image", full, errors):
        pyramid = open_tiff(reader(as_url(he_url))).series[0].levels
        errors += level_errors(root, "images/he_image", len(pyramid), full)
        for level in stored_levels(root["images/he_image"]):
            work.append((f"he_image s{level}", root[f"images/he_image/s{level}"], [("", pyramid[level].pages[0], ())]))
    total = sum(len(entries) for _, _, entries in work)
    with Progress(f"images: comparing {total:,} pages, four tiles each", total=total, unit="pages") as progress:
        for label, array, entries in work:
            errors += check_level(label, array, entries, progress)
    return errors


def apply_transform(attr: dict, axes: Sequence[str], point: dict[str, float]) -> dict[str, float]:
    """``point`` through the stored transform ``attr``, by axis name; an axis that ``attr`` lacks keeps its value.

    ``axes`` are the axes of a transform that states no input system, as a dataset's transform does.
    """
    kind = attr["type"]
    if kind == "sequence":
        for step in attr["transformations"]:
            point = apply_transform(step, axes, point)
        return point
    names = [a["name"] for a in attr["input"]["axes"]] if "input" in attr else list(axes)
    if kind == "scale":
        return {**point, **{n: point[n] * f for n, f in zip(names, attr["scale"], strict=True)}}
    if kind == "translation":
        return {**point, **{n: point[n] + t for n, t in zip(names, attr["translation"], strict=True)}}
    if kind == "affine":
        outputs = [a["name"] for a in attr["output"]["axes"]] if "output" in attr else names
        vector = [*(point[n] for n in names), 1.0]
        return {**point, **{o: sum(m * v for m, v in zip(row, vector, strict=True))
                            for o, row in zip(outputs, attr["affine"], strict=True)}}
    raise ValueError(f"unknown transform type {kind!r}")


def parse_alignment(text: str) -> list[list[float]]:
    """The matrix of the alignment CSV: 3 lines of 3 numbers, with CR LF or LF line ends."""
    rows = [[float(v) for v in line.split(",")] for line in text.splitlines() if line.strip()]
    if [len(row) for row in rows] != [3, 3, 3]:
        raise ValueError(f"the alignment has rows of {[len(row) for row in rows]} numbers, not 3 by 3")
    return rows


def aligned(matrix: Sequence[Sequence[float]], pixel_size: float, point: dict[str, float]) -> dict[str, float]:
    """The micrometres of the H&E pixel ``point``: ``matrix`` gives morphology pixels in ``(x, y)`` order."""
    vector = (point["x"], point["y"], 1.0)
    return {axis: pixel_size * sum(m * v for m, v in zip(row, vector)) for axis, row in zip("xy", matrix)}


def stack_z_step(ome_xml: str) -> float:
    """``PhysicalSizeZ`` of the first ``Pixels`` element of ``ome_xml``, in micrometres."""
    pixels = next(e for e in ET.fromstring(ome_xml).iter() if e.tag.rsplit("}", 1)[-1] == "Pixels")
    if (unit := pixels.get("PhysicalSizeZUnit", MICROMETRE[0])) not in MICROMETRE:
        raise ValueError(f"PhysicalSizeZ is in {unit}, not micrometres")
    if (size := pixels.get("PhysicalSizeZ")) is None:
        raise ValueError("the OME-XML of the stack has no PhysicalSizeZ")
    return float(size)


def _placement_errors(root, path: str, sizes: Sequence[dict[str, int]], expected: Callable) -> list[str]:
    """Each level that ``path`` lists maps its first pixel and its last to the place that the source gives.

    ``sizes`` has the source's size on each space axis, by level. ``expected`` takes a point of level 0, in pixels.
    The last pixel of level k and the last of level 0 are one place.
    """
    multiscale = root[path].attrs["ome"]["multiscales"][0]
    axes = [a["name"] for a in multiscale["axes"]]
    element = [t for t in multiscale.get("coordinateTransformations", ()) if t.get("output", {}).get("name") == "global"]
    if len(element) != 1:
        return [f"{path}: {len(element)} transforms into global"]
    errors = []
    for dataset in multiscale["datasets"]:
        label, level = f"{path} {dataset['path']}", int(dataset["path"].removeprefix("s"))
        if level >= len(sizes):
            errors.append(f"{label}: the source has {len(sizes)} levels")
            continue
        for scaled in (0, 1):
            pixel = {a: sizes[level].get(a, 0) * scaled for a in axes}
            found = pixel
            for step in (*dataset["coordinateTransformations"], *element):
                found = apply_transform(step, axes, found)
            wanted = expected({a: n * scaled for a, n in sizes[0].items()})
            errors += [f"{label}: pixel ({', '.join(f'{a} {v}' for a, v in pixel.items())}) maps to {a} {found[a]}, "
                       f"the source gives {value}" for a, value in wanted.items()
                       if not math.isclose(found[a], value, rel_tol=TRANSFORM_TOLERANCE)]
    print(f"  transforms: {path}: {_plural(len(multiscale['datasets']), 'level')}, the first and the last pixel of each")
    return errors


def _alignment_errors(group, matrix: list[list[float]]) -> list[str]:
    """The attribute ``alignment`` holds ``matrix`` in ``(x, y)`` order."""
    stored, errors = group.attrs.get("alignment", {}), []
    if stored.get("axes") != ["x", "y"]:
        errors.append(f"images/he_image: alignment axes {stored.get('axes')}, the CSV is in x, y order")
    found = stored.get("matrix")
    if np.shape(found) != (3, 3) or not all(
            math.isclose(f, m, rel_tol=TRANSFORM_TOLERANCE) for f, m in zip(np.ravel(found), np.ravel(matrix))):
        errors.append(f"images/he_image: alignment matrix {found}, the CSV gives {matrix}")
    return errors


def gate_transforms(root, bundle, open_he: Callable, alignment_text: Callable, *, full: bool) -> list[str]:
    """The place in ``global`` of the first and the last pixel of each level of each image and label element.

    Each expected place comes from the source, and the gate computes it without the ingest's transform code.
    ``open_he`` and ``alignment_text`` are called only when the store has the H&E element.
    A full store must hold each element; a fixture skips an element that it lacks.
    """
    errors, pixel_size = [], bundle.experiment()["pixel_size"]

    def plane(point):
        return {a: n * pixel_size for a, n in point.items()}

    if _present(root, "images/morphology", full, errors):
        levels = bundle.tiff(bundle.morphology_2d()[0]).series[0].levels
        errors += _placement_errors(root, "images/morphology", [dict(zip("yx", lv.shape[-2:])) for lv in levels], plane)
    if _present(root, "images/morphology_3d", full, errors):
        tif = bundle.tiff(bundle.morphology_3d())
        z_step = stack_z_step(tif.pages.first.description)
        sizes = [{"z": len(lv.pages), **dict(zip("yx", lv.shape[-2:]))} for lv in tif.series[0].levels]
        errors += _placement_errors(root, "images/morphology_3d", sizes,
                                    lambda point: {**plane(point), "z": point["z"] * z_step})
    if _present(root, "images/he_image", full, errors):
        matrix = parse_alignment(alignment_text())
        pages = [lv.pages[0] for lv in open_tiff(open_he()).series[0].levels]
        errors += _placement_errors(root, "images/he_image", [{"y": pg.imagelength, "x": pg.imagewidth} for pg in pages],
                                    lambda point: aligned(matrix, pixel_size, point))
        errors += _alignment_errors(root["images/he_image"], matrix)
    cells = bundle.member_zip(CELLS)
    for name, mask in MASKS.items():
        if _present(root, f"labels/{name}", full, errors):
            sizes = [dict(zip("yx", json.loads(cells.read(f"{mask}/.zarray"))["shape"]))]
            errors += _placement_errors(root, f"labels/{name}", sizes, plane)
    held = [name for name in QC_NAMES if _present(root, f"images/qc_{name}", full, errors)]
    if not held:
        return errors
    with bundle.member_file(bundle.report()) as f:
        plot = data_bag(f.read().decode())["density_plot"]
    extent, origin = plot["dimensions"], dict(zip("xy", plot["origin_data"]))
    for name in held:
        # A name that ends in a bin size has that bin; each other raster has the bin of the fine grid.
        bin_size = float(name[:-2].rsplit("_", 1)[-1]) if name.endswith("um") else extent["width"] / plot["horizontal_bin_count"]
        sizes = [{"y": math.ceil(extent["height"] / bin_size), "x": math.ceil(extent["width"] / bin_size)}]
        errors += _placement_errors(root, f"images/qc_{name}", sizes,
                                    lambda point, b=bin_size: {a: origin[a] + n * b for a, n in point.items()})
    return errors


def _transform_errors(path: str, raster, transform: dict) -> list[str]:
    scale_step, shift = transform["transformations"]
    errors = []
    if scale_step["scale"] != [1.0, raster.bin_size, raster.bin_size]:
        errors.append(f"{path}: scale {scale_step['scale']}, the report gives bin size {raster.bin_size}")
    if shift["translation"] != [0.0, raster.origin[1], raster.origin[0]]:
        errors.append(f"{path}: translation {shift['translation']}, the report gives origin {raster.origin}")
    return errors


def gate_qc(root, bundle, *, full: bool = False) -> list[str]:
    """Each stored QC raster against the report's, decoded again: values, dtype, shape, scale and translation.

    A full store must hold every raster of ``QC_NAMES``; a fixture skips a raster that it lacks.
    """
    errors, found = [], bundle_rasters(bundle)
    for name in QC_NAMES if full else found:
        path = f"images/qc_{name}"
        if not _present(root, path, full, errors):
            continue
        if name not in found:
            errors.append(f"{path}: the report has no such raster")
            continue
        raster = found[name]
        stored = root[f"{path}/s0"]
        if stored.shape != (1, *raster.array.shape) or stored.dtype != raster.array.dtype:
            errors.append(f"{path}: stored {stored.shape} {stored.dtype}, the report has "
                          f"{(1, *raster.array.shape)} {raster.array.dtype}")
        elif not np.array_equal(stored[0], raster.array, equal_nan=True):
            differ = ~((stored[0] == raster.array) | (np.isnan(stored[0]) & np.isnan(raster.array)))
            errors.append(f"{path}: {int(differ.sum())} values differ from the report")
        label = root[path].attrs["ome"]["omero"]["channels"][0]["label"]
        if label != raster.label:
            errors.append(f"{path}: channel label {label!r}, the report gives {raster.label!r}")
        transform = root[path].attrs["ome"]["multiscales"][0]["coordinateTransformations"][0]
        errors += _transform_errors(path, raster, transform)
    return errors


def compare_chunks(
    store, zf, prefix: str, path: str, label: str, progress: Progress, members: list[str] | None = None,
) -> list[str]:
    """Chunks of the v2 array at ``prefix`` against the stored array at ``path``, by hash: ``members``, or all.

    The stored chunk count must equal the member count, so a missing chunk fails even when it is not compared.
    """
    errors, ndim = [], len(json.loads(zf.read(f"{prefix}/.zarray"))["shape"])
    present = chunk_members(zf, prefix)
    # icechunk lists only from a node path, so list the array and keep its chunk keys.
    stored = sum(k == f"{path}/c" or k.startswith(f"{path}/c/") for k in sync(_list(store, path)))
    if stored != len(present):
        errors.append(f"{label}: {stored} chunks stored, {len(present)} members")
    for member in present if members is None else members:
        key = f"{path}/c" if ndim == 0 else chunk_key(path, prefix, member)
        found = raw(store, key)
        if found is None or _sha(found) != _sha(zf.read(member)):
            errors.append(f"{label}: chunk {member} differs")
        progress.advance()
    return errors


def gate_labels(root, cells) -> list[str]:
    """Every chunk of each mask against the source member, by hash."""
    errors = []
    for name, mask in MASKS.items():
        path, total = f"labels/{name}/s0", len(chunk_members(cells, mask))
        if not total:
            errors.append(f"{mask} has no chunk members")
        with Progress(f"labels: comparing {total:,} of {total:,} chunks of {mask}", total=total, unit="chunks") as p:
            errors += compare_chunks(root.store, cells, mask, path, path, p)
    return errors


async def _list(store, prefix):
    return [k async for k in store.list_prefix(prefix)]


def _metadata_errors(array, zarray: dict, label: str) -> list[str]:
    """The stored array's shape, chunk grid, dtype, codecs and fill value are those that ``create_copy`` makes of ``zarray``."""
    expected = create_copy(zarr.group(), "a", zarray).metadata.to_dict()
    found = array.metadata.to_dict()
    differing = [k for k in ("shape", "chunk_grid", "data_type", "codecs", "fill_value") if found.get(k) != expected.get(k)]
    return [f"{label}: metadata differs from the source .zarray in {', '.join(differing)}"] if differing else []


def gate_matrix(root, matrix, *, full: bool = False) -> list[str]:
    """``X`` is CSR with the shape and stored values of the source.

    A full run compares every chunk of every array copied from the matrix store, and its metadata, with the source.
    A fixture run compares ``MATRIX_CHUNKS`` sampled chunks of ``X/data`` and ``X/indices``.
    """
    errors = []
    x = sparse_dataset(root["tables/table/X"])
    shape, nnz = tuple(json.loads(matrix.read("X/.zattrs"))["shape"]), json.loads(matrix.read("X/data/.zarray"))["shape"][0]
    stored = x.group["data"].shape[0]
    print(f"  matrix: {x.format}, shape {x.shape}, {stored:,} stored values")
    if x.format != "csr":
        errors.append(f"X is {x.format}, not CSR")
    if x.shape != shape or stored != nnz:
        errors.append(f"X has shape {x.shape} and {stored} stored values; the source has {shape} and {nnz}")
    rng = np.random.default_rng(SEED)
    if full:
        arrays = sorted(n.removesuffix("/.zarray") for n in matrix.namelist() if n.endswith("/.zarray"))
        chosen = {a: None for a in arrays}
    else:
        chosen = {}
        for part in ("data", "indices"):
            members = chunk_members(matrix, f"X/{part}")
            chosen[f"X/{part}"] = [members[i] for i in sorted(
                rng.choice(len(members), min(MATRIX_CHUNKS // 2, len(members)), replace=False))]
    total = sum(len(chunk_members(matrix, a)) if m is None else len(m) for a, m in chosen.items())
    with Progress(f"matrix: comparing {total:,} chunks of {len(chosen)} arrays", total=total, unit="chunks") as progress:
        for array, members in chosen.items():
            path = f"tables/table/{array}"
            if full:
                errors += _metadata_errors(root[path], json.loads(matrix.read(f"{array}/.zarray")), array)
            errors += compare_chunks(root.store, matrix, array, path, array, progress, members)
    return errors


def element_tables(root, *, full: bool = False) -> dict[str, dict]:
    """``iceberg_table`` attribute by element path.

    A full store must hold a table for each of the full tree's points and shapes elements; ``ValueError`` names the others.
    """
    found = {}
    for kind in ("points", "shapes"):
        for name, group in root[kind].groups() if kind in root else ():
            if "iceberg_table" in group.attrs:
                found[f"{kind}/{name}"] = dict(group.attrs["iceberg_table"])
    if full and (lacking := [e for e in full_elements() if e.startswith(("points/", "shapes/")) and e not in found]):
        raise ValueError(f"the store lacks a table for {', '.join(lacking)}")
    return found


def load(catalog, attr):
    return catalog.load_table(f"{attr['namespace']}.{attr['table']}")


def pinned_files(catalog, attr) -> list:
    """The data files of the snapshot that ``attr`` pins, not of the current snapshot."""
    return [task.file for task in load(catalog, attr).scan(snapshot_id=attr["snapshot_id"]).plan_files()]


def _row_group_errors(path: str, table, held: list, counts: dict, first: dict) -> list[str]:
    """Each tile's row groups, read from the Parquet footers only, chain from the tile's first ``instance``.

    A row group starts after the last, and its range holds as many values as it has rows.
    This proves contiguity at row-group granularity. It does not show a repeat inside one row group
    whose minimum, maximum and row count are right.
    """
    errors, groups = [], {}
    for tile, file in held:
        with table.io.new_input(file.file_path).open() as stream:
            groups.setdefault(tile, []).extend(
                (low, high, rows, n, _name(file.file_path)) for n, (low, high, rows) in enumerate(
                    row_group_bounds(stream, "instance")))
    for tile, found in groups.items():
        if tile not in counts or not counts[tile]:
            continue
        expected = first[tile]
        for low, high, rows, n, name in found:
            if low != expected:
                errors.append(f"{path}: tile {tile}, {name} row group {n} has instances {low}..{high}, expected from {expected}")
            elif high - low + 1 != rows:
                errors.append(f"{path}: tile {tile}, {name} row group {n} has {rows} rows for {high - low + 1} instances")
            expected = high + 1
        if (total := sum(g[2] for g in found)) != counts[tile]:
            errors.append(f"{path}: tile {tile} row groups hold {total} rows, the source {counts[tile]}")
    return errors


def _transcript_errors(path: str, catalog, attr, grid: dict) -> list[str]:
    """The pinned transcripts table against the source's tiles.

    The manifests give each tile's row count and its ``instance`` range in the source order.
    The footers give each row group's range. A range that fits the count can hide a repeat inside one row group.
    """
    table = load(catalog, attr)
    held = partition_files(table, attr["snapshot_id"])
    rows, bounds = partition_counts(held), partition_bounds(table, held, attr["index_column"], attr["snapshot_id"])
    counts = dict(zip(grid["grid_keys"], grid["grid_number_objects"], strict=True))
    first = dict(tile_order(grid))
    errors = [f"{path}: tile {t} is not in the source" for t in sorted(set(rows) - set(counts))]
    errors += [f"{path}: tile {t} has {rows.get(t, 0)} rows, the source {n}" for t, n in counts.items() if rows.get(t, 0) != n]
    for tile, (low, high) in bounds.items():
        if tile in counts and counts[tile] and (low, high) != (first[tile], first[tile] + counts[tile] - 1):
            errors.append(f"{path}: tile {tile} has instances {low}..{high}, the source order gives "
                          f"{first[tile]}..{first[tile] + counts[tile] - 1}")
    errors += _row_group_errors(path, table, held, counts, first)
    ordered = sorted(bounds.items(), key=lambda item: item[1])
    errors += [f"{path}: instance runs to {high} in tile {a} and starts at {low} in tile {b}"
               for (a, (_, high)), (b, (low, _)) in zip(ordered, ordered[1:]) if low != high + 1]
    total, expected = sum(rows.values()), sum(counts.values())
    print(f"  tables: {path}: {len(rows)} tiles, {total:,} rows, the source {expected:,}")
    if total != expected:
        errors.append(f"{path}: {total} rows, the source {expected}")
    return errors


def gate_tables(root, catalog, cells, transcripts, *, full: bool = False) -> list[str]:
    """Each table's row count against the source.

    A full store's transcripts are compared tile by tile. A fixture's are compared with tile ``FIXTURE_TILE``.
    """
    errors = []
    for path, attr in element_tables(root, full=full).items():
        # A polygon table has one row per cell with a polygon.
        if path in POLYGON_SETS:
            expected = len(np.unique(read_array(cells, f"polygon_sets/{POLYGON_SETS[path]}/cell_index")))
        elif full:
            errors += _transcript_errors(path, catalog, attr, json.loads(transcripts.read("grid/.zattrs")))
            continue
        else:
            expected = json.loads(transcripts.read(f"grid/{FIXTURE_TILE}/location/.zarray"))["shape"][0]
        rows = sum(f.record_count for f in pinned_files(catalog, attr))
        print(f"  tables: {path} has {rows:,} rows, the source {expected:,}")
        if rows != expected:
            errors.append(f"{path}: {rows} rows, source {expected}")
    return errors


def _field_id_errors(path: str, catalog, attr) -> list[str]:
    """The data files of the snapshot that ``attr`` pins state every field id of that snapshot's schema.

    The index column is required in that schema and non-nullable in each file.
    """
    table, index, snapshot_id = load(catalog, attr), attr["index_column"], attr["snapshot_id"]
    snapshot = table.snapshot_by_id(snapshot_id)
    if snapshot is None:
        return [f"{path}: the table has no snapshot {snapshot_id}"]
    schema = table.schemas()[snapshot.schema_id]
    expected, errors = index_by_name(schema), []
    if not schema.find_field(index).required:
        errors.append(f"{path}: {index} is optional in the table schema")
    files = [task.file.file_path for task in table.scan(snapshot_id=snapshot_id).plan_files()]
    if not files:
        errors.append(f"{path}: snapshot {snapshot_id} has no data files")
    for f in files:
        with table.io.new_input(f).open() as stream:
            footer = pq.read_schema(stream)
        found = parquet_field_ids(footer)
        if extra := [n for n in found if n not in expected]:
            errors.append(f"{path}: {_name(f)} has columns the table lacks: {', '.join(extra)}")
        if wrong := [n for n, i in found.items() if n in expected and i != expected[n]]:
            errors.append(f"{path}: {_name(f)} has no field id, or another, for {', '.join(wrong)}")
        if lacking := [n for n in expected if n not in found]:
            errors.append(f"{path}: {_name(f)} lacks fields {', '.join(lacking)}")
        if index in footer.names and footer.field(index).nullable:
            errors.append(f"{path}: {_name(f)} has a nullable {index}")
    print(f"  field_ids: {path}: snapshot {snapshot_id}, {len(files)} files, {len(expected)} fields")
    return errors


def gate_field_ids(root, catalog, *, full: bool = False) -> list[str]:
    attrs = element_tables(root, full=full)
    errors = [] if attrs else ["no element has an iceberg_table attribute"]
    for path, attr in attrs.items():
        errors += _field_id_errors(path, catalog, attr)
    return errors


def gate_table_index(root) -> list[str]:
    """``obs`` and ``var`` list their index array only as ``_index``, and anndata reads them."""
    errors = []
    for name in ("obs", "var"):
        group = root[f"tables/table/{name}"]
        index = group.attrs["_index"]
        if index in group.attrs["column-order"]:
            errors.append(f"{name}: index {index!r} is also in column-order")
        try:
            frame = read_elem(group)
        except Exception as e:  # The gate reports any reader failure as a mismatch.
            errors.append(f"{name}: read_elem raised {type(e).__name__}: {e}")
            continue
        print(f"  table_index: {name} reads as {frame.shape[0]:,} rows, {frame.shape[1]} columns, index {index!r}")
    return errors


def _pixel(micrometres: float, pixel_size: float) -> int:
    return int(micrometres / pixel_size)


def _area(labels, row: int, col: int, pixel_size: float) -> tuple[int, float]:
    """The label at ``(row, col)`` and its area in square micrometres within the window."""
    window = labels[max(row - WINDOW, 0):row + WINDOW, max(col - WINDOW, 0):col + WINDOW]
    label = int(labels[row, col])
    return label, float((window == label).sum()) * pixel_size**2 if label else math.nan


def gate_masks(root, summary, columns, pixel_size) -> list[str]:
    nucleus, cell = columns.index("nucleus_area"), columns.index("cell_area")
    ny, nx = columns.index("nucleus_centroid_y"), columns.index("nucleus_centroid_x")
    # A cell without a nucleus has NaN nucleus columns.
    with_nucleus = np.flatnonzero(np.isfinite(summary[:, [nucleus, ny, nx]]).all(axis=1))
    print(f"  masks: {len(summary) - len(with_nucleus):,} of {len(summary):,} cell_summary rows have no nucleus")
    rows = np.random.default_rng(SEED).choice(with_nucleus, SAMPLED, replace=False)
    errors = {}
    for name in MASKS:
        labels = root[f"labels/{name}/s0"]
        areas = np.array([_area(labels, _pixel(summary[r, ny], pixel_size), _pixel(summary[r, nx], pixel_size),
                                pixel_size)[1] for r in rows])
        for column, index in (("nucleus_area", nucleus), ("cell_area", cell)):
            relative = np.abs(areas - summary[rows, index]) / summary[rows, index]
            errors[name, column] = float(np.nanmedian(relative))
        print(f"  masks: {name}: median label area {np.nanmedian(areas):.2f} um2, background at "
              f"{int(np.isnan(areas).sum())} of {SAMPLED} nucleus centroids")
    print(f"  masks: cell_summary medians: nucleus_area {np.median(summary[rows, nucleus]):.2f} um2, "
          f"cell_area {np.median(summary[rows, cell]):.2f} um2")
    for (name, column), error in errors.items():
        print(f"  masks: {name} vs {column}: median relative error {error:.3f}")
    failures = []
    if not errors["nucleus_labels", "nucleus_area"] < errors["cell_labels", "nucleus_area"]:
        failures.append("nucleus_area agrees better with cell_labels than with nucleus_labels")
    if not errors["cell_labels", "cell_area"] < errors["nucleus_labels", "cell_area"]:
        failures.append("cell_area agrees better with nucleus_labels than with cell_labels")
    return failures


def gate_instances(root, summary, columns, cell_id, pixel_size) -> list[str]:
    rows = np.random.default_rng(SEED + 1).choice(len(summary), SAMPLED, replace=False)
    cy, cx = columns.index("cell_centroid_y"), columns.index("cell_centroid_x")
    labels = root["labels/cell_labels/s0"]
    obs_cell_id = root["tables/table/obs/cell_id"][:]
    instance = root[f"tables/table/obs/{INSTANCE_KEY}"][:]
    errors = []
    for r in rows:
        label = int(labels[_pixel(summary[r, cy], pixel_size), _pixel(summary[r, cx], pixel_size)])
        pair = (int(obs_cell_id[r]) & 0xFFFFFFFF, int(obs_cell_id[r]) >> 32)
        if label != r + 1:
            errors.append(f"row {r}: cell_labels at the cell centroid is {label}, not {r + 1}")
        if pair != tuple(int(v) for v in cell_id[r]):
            errors.append(f"row {r}: obs.cell_id {obs_cell_id[r]} is {pair}, cells cell_id is {tuple(cell_id[r])}")
        if instance[r] != r + 1:
            errors.append(f"row {r}: obs.{INSTANCE_KEY} is {instance[r]}, not {r + 1}")
    print(f"  instances: {SAMPLED} rows, {len(errors)} mismatches")
    return errors


def pinned_columns(catalog, attr, columns: tuple[str, ...]):
    """The ``columns`` of the snapshot that ``attr`` pins, as an Arrow table."""
    return load(catalog, attr).scan(snapshot_id=attr["snapshot_id"], selected_fields=columns).to_arrow()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _repeats(path: str, noun: str, values: np.ndarray) -> list[str]:
    """One error when ``values`` repeat, naming the first repeated value."""
    unique, counts = np.unique(values, return_counts=True)
    if not len(repeated := unique[counts > 1]):
        return []
    return [f"{path}: {_plural(len(repeated), noun)} repeat{'s' if len(repeated) == 1 else ''}, first {repeated[0]}"]


def _first_cell(label: str, cells: np.ndarray) -> list[str]:
    """One error that says how many ``cells`` differ, and the first."""
    return [f"{label} on {_plural(len(cells), 'cell')}, first cell_index {cells[0]}"] if len(cells) else []


def gate_polygons(root, catalog, cells, summary, columns, *, full: bool = False) -> list[str]:
    """Each polygon set against its source: unique ``instance``, every part counted, and the invalid parts.

    ``part_count`` equals the source rows of each cell. The nucleus set also equals ``nucleus_count`` of every cell.
    """
    errors = []
    for path, attr in ((p, a) for p, a in element_tables(root, full=full).items() if p in POLYGON_SETS):
        table = pinned_columns(catalog, attr, ("instance", "cell_index", "part_count", "geometry"))
        errors += _repeats(path, "instance", table["instance"].to_numpy())
        source = read_array(cells, f"polygon_sets/{POLYGON_SETS[path]}/cell_index").astype(np.int64)
        held = np.zeros(len(summary), dtype=np.int64)
        held[table["cell_index"].to_numpy()] = table["part_count"].to_numpy()
        if (total := int(table["part_count"].to_numpy().sum())) != len(source):
            errors.append(f"{path}: part_count sums to {total}, the source has {len(source)} rows")
        errors += _first_cell(f"{path}: part_count differs from the source row count",
                              np.flatnonzero(held != np.bincount(source, minlength=len(summary))))
        parts = shapely.get_parts(shapely.from_wkb(table["geometry"].to_numpy(zero_copy_only=False)))
        invalid = int((~shapely.is_valid(parts)).sum())
        print(f"  polygons: {path}: {len(table):,} cells, {total:,} parts, {invalid:,} parts not valid")
        if path == "shapes/nucleus_boundaries":
            errors += _nucleus_count_errors(path, held, summary[:, columns.index("nucleus_count")])
            stated = root.attrs["metrics"]["number_of_nucleus_non_simple_polygons"]
            if stated is None:
                # 10x leaves the metric empty, so there is nothing to disagree with.
                print(f"  polygons: FINDING {invalid:,} invalid nucleus polygons; the metrics CSV states no count")
            elif invalid != stated:
                errors.append(f"{invalid} invalid nucleus polygons, the metrics CSV states {stated}")
    return errors


def _nucleus_count_errors(path: str, held: np.ndarray, nucleus_count: np.ndarray) -> list[str]:
    """``part_count`` against ``nucleus_count`` of every cell; a cell with no row has 0 parts."""
    unknown = np.isnan(nucleus_count)
    return (_first_cell("cell_summary nucleus_count is NaN", np.flatnonzero(unknown))
            + _first_cell(f"{path}: part_count differs from nucleus_count",
                          np.flatnonzero(~unknown & (held != np.nan_to_num(nucleus_count)))))


def gate_table_region(root, catalog, *, full: bool = False) -> list[str]:
    """The table's ``instance`` set equals ``cell_boundaries``'; ``cell_id`` agrees on a sample; the region is ``cell_boundaries``."""
    attrs = element_tables(root, full=full)
    if "shapes/cell_boundaries" not in attrs:
        return ["no shapes/cell_boundaries element"]
    errors, table = [], root["tables/table"]
    polygons = pinned_columns(catalog, attrs["shapes/cell_boundaries"], ("instance", "cell_id"))
    shape_instance, instance = polygons["instance"].to_numpy(), table[f"obs/{INSTANCE_KEY}"][:]
    for noun, only, other in (("obs.instance", np.setdiff1d(instance, shape_instance), "cell_boundaries"),
                              ("cell_boundaries", np.setdiff1d(shape_instance, instance), "obs.instance")):
        if len(only):
            errors.append(f"{noun} has {_plural(len(only), 'instance')} that {other} lacks, first {only[0]}")
    values, counts = np.unique(instance, return_counts=True)
    if len(repeated := values[counts > 1]):
        errors.append(f"obs.instance has {_plural(len(repeated), 'instance')} that repeats, first {repeated[0]}")
    known = np.flatnonzero(np.isin(instance, shape_instance))
    rows = np.random.default_rng(SEED + 3).choice(known, min(SAMPLED, len(known)), replace=False)
    packed = table["obs/cell_id"][:][rows]
    # obs.cell_id packs the pair as prefix + suffix * 2**32.
    obs_ids = xenium_cell_id(packed & 0xFFFFFFFF, packed >> 32)
    shape_ids = dict(zip(shape_instance.tolist(), polygons["cell_id"].to_pylist()))
    differ = sorted(int(instance[r]) for r, a in zip(rows, obs_ids) if shape_ids[int(instance[r])] != a)
    if differ:
        errors.append(f"cell_id differs on {len(differ)} of {len(rows)} sampled instances, first instance {differ[0]}")
    for label, found in (
        ("region attribute", table.attrs.get("region")),
        ("uns region", table["uns/spatialdata_attrs/region"][()]),
    ):
        if found != TABLE_REGION:
            errors.append(f"tables/table: {label} is {found!r}")
    regions = {str(r) for r in read_elem(table["obs/region"])}
    if regions != {TABLE_REGION}:
        errors.append(f"tables/table: obs.region holds {', '.join(repr(r) for r in sorted(regions))}")
    return errors


def gate_extra_tables(root, catalog, transcripts, bundle) -> list[str]:
    """Each table of the root attribute ``iceberg_tables`` against its source.

    The pin is in the catalog, the row count equals the source, the index has no repeat, and the files carry field ids.
    """
    pins = root.attrs.get(ICEBERG_TABLES)
    if not pins:
        return [f"the root has no {ICEBERG_TABLES} attribute"]
    expected = {"codewords": len(json.loads(transcripts.read(".zattrs"))["codeword_gene_names"])}
    for name, member in DIFFEXP_TABLES.items():
        with bundle.member_file(member) as f:
            # The header is not a data row.
            expected[name] = sum(1 for _ in csv.reader(io.TextIOWrapper(f, encoding="utf-8", newline=""))) - 1
    errors = [f"{ICEBERG_TABLES} has no entry for {name}" for name in expected if name not in pins]
    for name, attr in pins.items():
        path = f"{ICEBERG_TABLES}/{name}"
        if name not in expected:
            errors.append(f"{path}: the source has no such table")
        elif found := _attr_errors(path, catalog, attr):
            errors += found
        else:
            rows = sum(f.record_count for f in pinned_files(catalog, attr))
            print(f"  extra_tables: {path} has {rows:,} rows, the source {expected[name]:,}")
            if rows != expected[name]:
                errors.append(f"{path}: {rows} rows, source {expected[name]}")
            index = attr["index_column"]
            errors += _repeats(path, f"{index} value", pinned_columns(catalog, attr, (index,))[index].to_numpy(zero_copy_only=False))
            errors += _field_id_errors(path, catalog, attr)
    return errors


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def copy_orders(transcripts) -> dict[str, list[str]]:
    """The tile of each data file of a copied table, in the order of the copy's arrays: the source tiles with rows."""
    grid = json.loads(transcripts.read("grid/.zattrs"))
    counts = dict(zip(grid["grid_keys"], grid["grid_number_objects"], strict=True))
    return {"points/transcripts": [t for t, _ in tile_order(grid) if counts[t]]}


def _copy_arrays(group) -> list:
    """The ``uint8`` copies of an element, in part order."""
    found = [a for _, a in group.members(max_depth=None) if isinstance(a, zarr.Array) and "file:name" in a.attrs]
    return sorted(found, key=lambda a: int(a.basename.split(".")[1]) if a.basename.startswith("part.") else 0)


def _copy_files(catalog, attr, order: list | None) -> list[str]:
    """The pinned snapshot's data file locations, by ``order`` of their partition values when given."""
    if order is None:
        return [f.file_path for f in pinned_files(catalog, attr)]
    rank = {value: i for i, value in enumerate(order)}
    held = partition_files(load(catalog, attr), attr["snapshot_id"])
    return [f.file_path for _, f in sorted(held, key=lambda item: rank.get(item[0], len(rank)))]


def _read_range(stream, start: int, stop: int) -> bytes:
    stream.seek(start)
    return stream.read(stop - start)


def gate_copies(root, catalog, orders: dict | None = None, *, full: bool = False) -> list[str]:
    """Each ``uint8`` copy against its table's data file, read directly, on a bounded sample of blocks.

    The copies hold as many bytes as the files, and each copy's chunk edges are the file's reference boundaries.
    A block is ``COPY_CHUNK`` bytes. Each copy compares its first block, its last and ``COPY_SAMPLES`` between,
    by hash; a changed byte in another block is not found. ``orders`` gives the partition order of a partitioned table's files.
    """
    errors, checks = [], []
    for path, attr in element_tables(root, full=full).items():
        io = load(catalog, attr).io
        arrays, files = _copy_arrays(root[path]), _copy_files(catalog, attr, (orders or {}).get(path))
        print(f"  copies: {path}: {len(arrays)} uint8 arrays, {len(files)} registered files")
        if len(arrays) != len(files):
            errors.append(f"{path}: {len(arrays)} uint8 arrays, {len(files)} data files")
            continue
        sizes = [len(io.new_input(f)) for f in files]
        if (copied := sum(a.shape[0] for a in arrays)) != sum(sizes):
            errors.append(f"{path}: the copies have {copied:,} bytes, the data files {sum(sizes):,}")
        for array, file, size in zip(arrays, files, sizes):
            if list(array.read_chunk_sizes[0]) != copy_edges(size):
                errors.append(f"{path}: {array.basename}: the chunk edges are not the {COPY_CHUNK // MIB} MiB "
                              f"references of a {size:,}-byte file")
            if array.shape[0] != size:
                errors.append(f"{path}: {array.basename} has {array.shape[0]:,} bytes, {_name(file)} has {size:,}")
            else:
                checks.append((path, array, io, file, size))
    total = sum(len(sampled_blocks(size, COPY_CHUNK, COPY_SAMPLES)) for *_, size in checks)
    with Progress(f"copies: comparing {total:,} blocks of {COPY_CHUNK // MIB} MiB", total=total, unit="blocks") as progress:
        for path, array, io, file, size in checks:
            with io.new_input(file).open() as stream:
                sampled, differing = compare_blocks(
                    lambda start, stop: array[start:stop].tobytes(), lambda start, stop: _read_range(stream, start, stop),
                    size, block=COPY_CHUNK, samples=COPY_SAMPLES, progress=progress)
            if differing:
                errors.append(f"{path}: {_name(file)}: {len(differing)} of {len(sampled)} sampled blocks differ, "
                              f"first block {differing[0]}")
    return errors


def _attr_errors(path: str, catalog, attr) -> list[str]:
    """The catalog's table has the UUID and the snapshot that ``attr`` pins."""
    table = load(catalog, attr)
    if str(table.metadata.table_uuid) != attr["table_uuid"]:
        return [f"{path}: attribute pins table {attr['table_uuid']}, catalog has {table.metadata.table_uuid}"]
    if table.snapshot_by_id(attr["snapshot_id"]) is None:
        return [f"{path}: the table has no snapshot {attr['snapshot_id']}"]
    return []


def gate_attrs(root, catalog, *, full: bool = False) -> list[str]:
    attrs = element_tables(root, full=full)
    print(f"  attrs: {len(attrs)} iceberg_table attributes")
    errors = [] if attrs else ["no element has an iceberg_table attribute"]
    for path, attr in attrs.items():
        errors += _attr_errors(path, catalog, attr)
    return errors


def _pin_errors(root) -> list[str]:
    """The root attribute ``iceberg_tables`` has an entry for each of ``EXTRA_TABLES``."""
    pins = root.attrs.get(ICEBERG_TABLES)
    if pins is None:
        return [f"{ICEBERG_TABLES}: missing"]
    return [f"{ICEBERG_TABLES}/{name}: missing" for name in EXTRA_TABLES if name not in pins]


def _element_errors(root, element: str) -> list[str]:
    """``element`` is on the branch and complete: marked, or pinned to its table."""
    if element == ICEBERG_TABLES:
        return _pin_errors(root)
    if element not in root:
        return [f"{element}: missing"]
    attrs = root[element].attrs
    if element.startswith(("images/", "labels/")) and attrs.get(COMPLETE) is not True:
        return [f"{element}: not marked {COMPLETE}"]
    if element.startswith(("points/", "shapes/")) and "iceberg_table" not in attrs:
        return [f"{element}: has no iceberg_table attribute"]
    # tables/table is not an Iceberg table; the region is the last attribute that its unit writes.
    if element == "tables/table" and "region" not in attrs:
        return [f"{element}: has no region attribute"]
    return []


def gate_complete(root) -> list[str]:
    """Every element of the full tree is on the branch and complete, and ``missing`` lists what the full mode leaves out."""
    errors = []
    for element in full_elements():
        errors += _element_errors(root, element)
    listed = root.attrs.get("missing", [])
    errors += [f"missing: lacks {m['what']}" for m in MISSING_FROM_FULL if m not in listed]
    errors += [f"missing: lists {m.get('what')}, which the full mode writes" for m in listed if m not in MISSING_FROM_FULL]
    print(f"  complete: {len(full_elements())} elements of the full tree, {len(MISSING_FROM_FULL)} listed as missing")
    return errors


def http_head(url: str) -> tuple[int, str]:
    """The size and the ETag, without its quotes, that the HEAD response for ``url`` gives."""
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as response:
        return int(response.headers["Content-Length"]), response.headers["ETag"].strip('"')


def gate_source(root, sample, layouts: str, head=http_head) -> list[str]:
    """Each source object's size and ETag from ``head`` equal LAYOUTS.md and the root attribute ``source``."""
    recorded = source_objects(layouts, sample)
    held = {o["url"]: o for o in root.attrs.get("source", [])}
    errors = [f"{url}: the root attribute source lists an object that LAYOUTS.md lacks"
              for url in held if url not in {o["url"] for o in recorded}]
    for obj in recorded:
        size, etag = head(obj["url"])
        print(f"  source: {obj['url']}: {size:,} bytes, ETag {etag}")
        if size != obj["size"]:
            errors.append(f"{obj['url']}: size {size}, LAYOUTS.md records {obj['size']}")
        if etag != obj["etag"]:
            errors.append(f"{obj['url']}: ETag {etag}, LAYOUTS.md records {obj['etag']}")
        if held.get(obj["url"]) != obj:
            errors.append(f"{obj['url']}: the root attribute source differs from LAYOUTS.md")
    return errors


def without_source(names: list[str], *, local: bool) -> list[str]:
    """``names`` without ``source`` for a local bundle, with a printed line: the gate needs the network."""
    if local and "source" in names:
        print("source: skipped; the gate sends HEAD requests, and a local bundle has no source object to ask")
        return [n for n in names if n != "source"]
    return names


def run_gates(gates: dict, names: list[str]) -> list[str]:
    """Run the gates ``names``, print each result, and return the failed names. A gate that raises fails."""
    failed = []
    for name in names:
        try:
            errors = gates[name]()
        except Exception as e:  # A gate that cannot run on this store is a failed gate, and the run goes on.
            errors = [f"raised {type(e).__name__}: {e}"]
        print(f"{name} {'FAIL' if errors else 'PASS'}", flush=True)
        for error in errors[:20]:
            print(f"  {error}")
        if errors:
            failed.append(name)
    return failed


def select_gates(requested: list[str] | None, available) -> list[str]:
    """The gates of ``--gates``, split at commas, or all of ``available``; an unknown name raises ``ValueError``."""
    if not requested:
        return list(available)
    names = [n for item in requested for n in item.split(",") if n]
    if unknown := [n for n in names if n not in available]:
        raise ValueError(f"unknown gates: {', '.join(unknown)}; known: {', '.join(available)}")
    return names


def open_root(location: str, branch: str) -> zarr.Group:
    """The branch read-only; both openers authorize the repo's virtual chunk containers."""
    if location.startswith(ARRAYLAKE):
        return zarr.open_group(arraylake_repository(location).readonly_session(branch).store, mode="r")
    return open_repo(location, branch=branch)


def open_catalog(location: str):
    if (org := catalog_org(location)) is not None:
        from arraylake import Client

        return Client().get_iceberg(org)
    return local_catalog(Path(location))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="a local directory, or arraylake://<org>/<repo>")
    parser.add_argument("--catalog", required=True, help="a local catalog directory, or arraylake://<org>")
    parser.add_argument("--mode", choices=("fixture", "full"), default="fixture",
                        help="fixture skips an element that the branch lacks; full fails on it")
    parser.add_argument("--branch", help="the branch to read; by default fixture for --mode fixture, main for --mode full")
    parser.add_argument("--sample", choices=sorted(SAMPLES), required=True)
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    parser.add_argument("--he-image", help="the H&E OME-TIFF, a URL or a local path; by default 10x's object")
    parser.add_argument("--he-alignment", help="the H&E alignment CSV, a URL or a local path; by default 10x's object")
    parser.add_argument("--record", type=Path,
                        help="--mode full: the record of the sample's bundle; by default the sample's committed record")
    parser.add_argument("--gates", nargs="+", help="run only these gates, comma-separated; by default all")
    parser.add_argument("--head-source", action="store_true",
                        help="run the source gate, which sends HEAD requests, although --bundle is a local file")
    args = parser.parse_args()
    require_same_place(args.repo, args.catalog)

    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    zarr.config.set({"array.rectilinear_chunks": True})
    full = args.mode == "full"
    root = open_root(args.repo, args.branch or {"fixture": "fixture", "full": "main"}[args.mode])
    catalog = open_catalog(args.catalog)
    sample = SAMPLES[args.sample]
    bundle, counter = open_bundle(args.bundle or sample.url(BUNDLE, S3))
    cells, matrix = bundle.member_zip(CELLS), bundle.member_zip(MATRIX)
    transcripts = bundle.member_zip(TRANSCRIPTS)
    pixel_size = bundle.experiment()["pixel_size"]
    names = set(cells.namelist())
    summary = read_array(cells, "cell_summary", names=names)
    columns = json.loads(cells.read("cell_summary/.zattrs"))["column_names"]
    cell_id = read_array(cells, "cell_id", names=names)

    he_image = args.he_image or sample.url(HE_IMAGE, S3)
    open_he, alignment_text = he_readers(he_image, args.he_alignment or sample.url(HE_ALIGNMENT, S3),
                                         counter.wrap, counter.wrap)
    gates = {
        "images": lambda: gate_images(root, bundle, he_image, full=full),
        "transforms": lambda: gate_transforms(root, bundle, open_he, alignment_text, full=full),
        "qc": lambda: gate_qc(root, bundle, full=full),
        "labels": lambda: gate_labels(root, cells),
        "matrix": lambda: gate_matrix(root, matrix, full=full),
        "tables": lambda: gate_tables(root, catalog, cells, transcripts, full=full),
        "masks": lambda: gate_masks(root, summary, columns, pixel_size),
        "instances": lambda: gate_instances(root, summary, columns, cell_id, pixel_size),
        "polygons": lambda: gate_polygons(root, catalog, cells, summary, columns, full=full),
        "table_region": lambda: gate_table_region(root, catalog, full=full),
        "copies": lambda: gate_copies(root, catalog, copy_orders(transcripts) if full else None, full=full),
        "attrs": lambda: gate_attrs(root, catalog, full=full),
        "extra_tables": lambda: gate_extra_tables(root, catalog, transcripts, bundle),
        "field_ids": lambda: gate_field_ids(root, catalog, full=full),
        "table_index": lambda: gate_table_index(root),
    }
    if full:
        gates = {
            "bundle": lambda: check(bundle, open_he, alignment_text, args.sample,
                                    record=load_record(args.sample, path=args.record), skip=INGEST_SKIPS),
            "complete": lambda: gate_complete(root), **gates,
            "source": lambda: gate_source(root, sample, LAYOUTS.read_text()),
        }
    try:
        names = select_gates(args.gates, gates)
    except ValueError as e:
        raise SystemExit(str(e))
    failed = run_gates(gates, without_source(names, local=bool(args.bundle) and not args.head_source))
    print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {counter.requests:,} requests")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
