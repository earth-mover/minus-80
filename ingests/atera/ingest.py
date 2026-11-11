"""Write an Atera sample into an Icechunk repo and an Iceberg catalog, local or on Arraylake.

The script has two modes. ``fixture`` writes the fixture of PLAN.md, "Steps, scope and risk", step 1.
``full`` writes every element of PLAN.md, "The tree", except the ones that ``MISSING_FROM_FULL`` lists.
Each mode writes onto a branch, one commit per element, or per level for an image.
The bundle is read in place by range requests.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import tempfile
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import zarr
from anndata.io import write_elem
from minus80.core.elements import ElementWriter
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo
from minus80.core.progress import MIB
from minus80.core.registry import as_url
from minus80.core.remote import ReadCounter, counting_reader, reader
from minus80.core.pyramid import channel_names, create_level, physical_sizes, write_page, write_stack
from minus80.core.tables import TableWriter, declare_copy, iceberg_table_attr, local_catalog
from minus80.core.zarr_copy import copy_chunks, copy_group, create_copy, read_array
from minus80.ome import channel_omero, image_attrs, label_attrs, rgb_omero
from minus80.ome.spatialdata import (
    affine, points_attrs, raster_attrs, scale, sequence, shapes_attrs, write_table_keys,
)

from bundle import (
    CELLS, DIFFEXP_AZIMUTH, DIFFEXP_GRAPHCLUST, MATRIX, MORPHOLOGY_2D, MORPHOLOGY_3D, TRANSCRIPTS, Bundle, open_tiff,
)
from polygons import GEO_POLYGONS, POLYGON_SCHEMA, polygon_table
from root_attrs import LICENSE, citation_text, parse_metrics, root_attrs, source_objects
from sources import HE_ALIGNMENT, HE_IMAGE, S3, SAMPLES
from tables_extra import codewords_table, diffexp_table
from transcripts import TRANSCRIPT_SCHEMA, tile_order, write_tile, xenium_cell_id

LAYOUTS = Path(__file__).with_name("LAYOUTS.md")

#: Label element, then the cells store's mask array; PLAN.md, judgement call 1.
MASKS = {"nucleus_labels": "masks/0", "cell_labels": "masks/1"}
NUCLEUS_SET, CELL_SET = 0, 1
#: The root attribute that pins the tables that annotate no element, each as an ``iceberg_table`` attribute does.
ICEBERG_TABLES = "iceberg_tables"
#: The Iceberg tables the fixture writes; the others wait for the full ingest.
FIXTURE_TABLES = ("nucleus_boundaries", "cell_boundaries", "transcripts")
#: Each differential expression table with its bundle member.
DIFFEXP_TABLES = {"diffexp_azimuth": DIFFEXP_AZIMUTH, "diffexp_graphclust": DIFFEXP_GRAPHCLUST}
FIXTURE_LEVEL = 6
FIXTURE_TILE = "0,0"
REGION_KEY, INSTANCE_KEY = "region", "instance"
#: The one element that the table annotates; PLAN.md, "The tables".
TABLE_REGION = "cell_boundaries"
#: The unit of the global coordinate system; PLAN.md, judgement call 4.
UNIT = "micrometer"
#: The coordinate system between the H&E alignment and the scale into ``global``.
MORPHOLOGY_PIXEL = "morphology_pixel"
#: The branches that each mode refuses to write.
PROTECTED = {"fixture": ("main",), "full": ("fixture",)}
#: The elements each mode writes, in order. In full mode the catalog elements come first, so a fault shows
#: before the hours of image writes. The root attributes come last, as they describe the branch.
ELEMENTS = {
    "fixture": ("tables/table", *(f"labels/{n}" for n in MASKS), "images/morphology", "shapes/nucleus_boundaries",
                "shapes/cell_boundaries", "points/transcripts", "root"),
    "full": ("tables/table", *(f"labels/{n}" for n in MASKS), "shapes/nucleus_boundaries", "shapes/cell_boundaries",
             "points/transcripts", ICEBERG_TABLES, "images/morphology", "images/morphology_3d", "images/he_image",
             "root"),
}
#: The elements ``--rewrite`` can write again; ``tables/table:attrs`` is the ``obs`` and ``var`` attributes.
REWRITABLE = ("points/transcripts", "shapes/nucleus_boundaries", "tables/table:attrs")

FIXTURE = (
    "This branch is a fixture, not the full store. It holds one level of images/morphology (s6), "
    f"one transcript tile (grid {FIXTURE_TILE}) and both polygon sets (nucleus and cell)."
)

#: What neither mode writes.
NOT_WRITTEN = [
    {"what": "csc_cell_feature_matrix.zarr.zip", "why": "It is a copy of X ordered by column."},
    {"what": "binned_transcripts.zarr.zip", "why": "It is derived from the transcripts for 10x Explorer."},
    {"what": "the HTML reports' thumbnails, plots and interface", "why": "They are not data."},
]

MISSING_FROM_FIXTURE = [
    {"what": "images/morphology levels 0 to 5 and 7", "why": "The fixture writes level 6 only."},
    {"what": "images/morphology_3d, images/he_image, images/overview_scan, images/qc_*", "why": "The fixture leaves them out."},
    {"what": f"points/transcripts tiles other than {FIXTURE_TILE}", "why": "The fixture writes one tile."},
    {"what": "Iceberg tables codewords, diffexp_azimuth, diffexp_graphclust", "why": "The fixture leaves them out."},
    *NOT_WRITTEN,
]

MISSING_FROM_FULL = [
    {"what": "images/overview_scan", "why": "The ingest does not write it. PLAN.md, judgement call 6, says when it can be placed."},
    {"what": "images/qc_*", "why": "The ingest does not write them."},
    *NOT_WRITTEN,
]


def open_bundle(location: str) -> tuple[Bundle, ReadCounter]:
    file, counter = counting_reader(as_url(location))
    return Bundle(location, file), counter


def member_bytes(zf, prefix: str) -> int:
    return sum(i.compress_size for i in zf.infolist() if i.filename.startswith(prefix))


def skip_held(writer: ElementWriter, path: str) -> bool:
    """True, with a printed line, when the branch holds ``path`` from an earlier run."""
    if writer.has(path):
        print(f"{path}: on branch {writer.branch} already, skipped", flush=True)
        return True
    return False


def unless_held(writer: ElementWriter, path: str, write):
    """A step that calls ``write`` unless the branch holds ``path`` from an earlier run."""
    return lambda: None if skip_held(writer, path) else write()


def drop_index_column(frame: zarr.Group) -> None:
    """Remove the index from the data frame's ``column-order``; the index array stays.

    The source lists the index in ``column-order`` too, so a reader sees the field twice.
    """
    frame.attrs["column-order"] = [c for c in frame.attrs["column-order"] if c != frame.attrs["_index"]]


def write_table(writer: ElementWriter, bundle: Bundle) -> None:
    zf = bundle.member_zip(MATRIX)
    with writer.unit("tables/table", "Write tables/table", total=member_bytes(zf, "")) as (root, _):
        group = root.require_group("tables").create_group("table")
        copy_group(zf, "", group)
        drop_index_column(group["obs"])
        drop_index_column(group["var"])
        obs = group["obs"]
        n = group["X"].attrs["shape"][0]
        # SpatialData needs a region column and the mask label as the instance key; the source has neither.
        write_elem(obs, REGION_KEY, pd.Categorical([TABLE_REGION] * n))
        write_elem(obs, INSTANCE_KEY, np.arange(1, n + 1, dtype=np.int64))
        obs.attrs["column-order"] = [*obs.attrs["column-order"], REGION_KEY, INSTANCE_KEY]
        write_table_keys(group, TABLE_REGION, region_key=REGION_KEY, instance_key=INSTANCE_KEY)


def write_table_index(writer: ElementWriter) -> None:
    """Write the ``obs`` and ``var`` attributes of a held ``tables/table`` again, index only."""
    with writer.unit("tables/table obs and var attributes", "Write tables/table obs and var attributes") as (root, _):
        drop_index_column(root["tables/table/obs"])
        drop_index_column(root["tables/table/var"])


def _level_paths(ome: dict, levels: Sequence[int]) -> dict:
    """``ome`` with its datasets stored at the paths of ``levels``."""
    for dataset, level in zip(ome["multiscales"][0]["datasets"], levels, strict=True):
        dataset["path"] = f"s{level}"
    return ome


def write_labels(
    writer: ElementWriter, bundle: Bundle, pixel_size: float, *, concurrency: int, names: Sequence[str] = tuple(MASKS),
) -> None:
    zf = bundle.member_zip(CELLS)
    for name in names:
        mask = MASKS[name]
        if skip_held(writer, f"labels/{name}"):
            continue
        with writer.unit(f"labels/{name}", f"Write labels/{name}", total=member_bytes(zf, f"{mask}/")) as (root, _):
            group = root.require_group("labels").create_group(name)
            array = create_copy(group, "s0", json.loads(zf.read(f"{mask}/.zarray")), dimension_names=("y", "x"))
            copy_chunks(zf, mask, array, concurrency=concurrency)
            ome = label_attrs(name=name, dims=("y", "x"), level_shapes=[array.shape],
                              scale={"y": 1.0, "x": 1.0}, source_image="../../images/morphology")
            group.attrs.update(raster_attrs(_level_paths(ome, [0]), scale([pixel_size] * 2, ("y", "x"), unit=UNIT)))


def write_levels(writer: ElementWriter, path: str, count: int, levels: Sequence[int] | None, write_level, attrs) -> None:
    """Write the image at ``path`` one level per unit, in ascending order, and skip a level the branch holds.

    ``write_level(group, level)`` writes the array and ``attrs(held)`` gives the group attributes for
    the levels held. Each commit lists exactly the levels it holds, so every commit is a valid store.
    ``levels`` of ``None`` means all ``count`` levels, and the element is marked complete after the last.
    """
    wanted = sorted(range(count) if levels is None else levels)
    held = [k for k in wanted if skip_held(writer, f"{path}/s{k}")]
    for level in (k for k in wanted if k not in held):
        with writer.unit(f"{path}/s{level}", f"Write {path}/s{level}") as (root, _):
            group = root
            for part in path.split("/"):
                group = group.require_group(part)
            write_level(group, level)
            held = sorted([*held, level])
            group.attrs.update(attrs(held))
    if levels is None and not writer.complete(path):
        writer.finish(path, f"Mark {path} complete")


def level_ome(name: str, dims: Sequence[str], shapes: Sequence[Sequence[int]], held: Sequence[int], omero: dict) -> dict:
    """The ``ome`` attributes of an image whose levels ``held`` are stored; ``shapes`` has every level of the source."""
    ome = image_attrs(name=name, dims=dims, level_shapes=[shapes[k] for k in held], scale={d: 1.0 for d in dims},
                      omero=omero)
    return _level_paths(ome, held)


def level_scale(dims: Sequence[str], shapes: Sequence[Sequence[int]], held: Sequence[int], pixel: dict[str, float]) -> dict:
    """The transform into ``global`` that gives the first level held its own pixel size; ``pixel`` is level 0's."""
    first, full = shapes[held[0]], shapes[0]
    return scale([pixel[d] * (full[i] / first[i]) for i, d in enumerate(dims)], dims, unit=UNIT)


def file_pixel_sizes(tif, pixel_size: float, label: str) -> dict[str, float]:
    """The micrometre sizes in the OME-XML of ``tif``; the sizes on ``x`` and ``y`` must equal ``pixel_size``."""
    sizes = physical_sizes(tif.pages.first.description)
    for axis in "xy":
        if axis not in sizes or not math.isclose(sizes[axis], pixel_size, rel_tol=1e-6):
            raise ValueError(f"{label}: pixel size {sizes.get(axis)} on {axis} differs from the experiment's {pixel_size}")
    return sizes


def write_morphology(
    writer: ElementWriter, bundle: Bundle, pixel_size: float, *, levels: Sequence[int] | None = None, concurrency: int,
) -> None:
    tiffs = [bundle.tiff(name) for name in MORPHOLOGY_2D]
    for name, tif in zip(MORPHOLOGY_2D, tiffs, strict=True):
        file_pixel_sizes(tif, pixel_size, name)
    pyramids = [tif.series[0].levels for tif in tiffs]
    shapes = [(len(tiffs), *level.shape) for level in pyramids[0]]
    names = channel_names(tiffs[0].pages.first.description)
    omero = channel_omero([{"label": n} for n in names], max_value=np.iinfo(pyramids[0][0].dtype).max)

    def write_level(group, level):
        pages = [pyramid[level].pages[0] for pyramid in pyramids]
        array = create_level(group, f"s{level}", shapes[level], pages[0].dtype, dims=("c", "y", "x"),
                             chunk=(pages[0].tilelength, pages[0].tilewidth))
        for channel, page in enumerate(pages):
            write_page(array, page, (channel,), concurrency=concurrency)

    write_levels(writer, "images/morphology", len(shapes), levels, write_level,
                 lambda held: raster_attrs(level_ome("morphology", ("c", "y", "x"), shapes, held, omero),
                                           level_scale(("c", "y", "x"), shapes, held,
                                                       {"c": 1.0, "y": pixel_size, "x": pixel_size})))


def write_morphology_3d(
    writer: ElementWriter, bundle: Bundle, pixel_size: float, *, levels: Sequence[int] | None = None, concurrency: int,
) -> None:
    tif = bundle.tiff(MORPHOLOGY_3D)
    z_step = file_pixel_sizes(tif, pixel_size, MORPHOLOGY_3D).get("z")
    if z_step is None:
        raise ValueError(f"{MORPHOLOGY_3D}: the OME-XML has no PhysicalSizeZ")
    # One series holds every plane: its level k is a ``(z, y, x)`` array with one page for each plane.
    pyramid = tif.series[0].levels
    shapes = [(1, len(level.pages), *level.shape[1:]) for level in pyramid]
    names = channel_names(tif.pages.first.description)
    omero = channel_omero([{"label": n} for n in names], max_value=np.iinfo(pyramid[0].dtype).max)

    def write_level(group, level):
        pages = list(pyramid[level].pages)
        array = create_level(group, f"s{level}", shapes[level], pages[0].dtype, dims=("c", "z", "y", "x"),
                             chunk=(pages[0].tilelength, pages[0].tilewidth), shard_planes=len(pages))
        write_stack(array, pages, (0,), concurrency=concurrency)

    write_levels(writer, "images/morphology_3d", len(shapes), levels, write_level,
                 lambda held: raster_attrs(level_ome("morphology_3d", ("c", "z", "y", "x"), shapes, held, omero),
                                           level_scale(("c", "z", "y", "x"), shapes, held,
                                                       {"c": 1.0, "z": z_step, "y": pixel_size, "x": pixel_size})))


def read_alignment(text: str) -> np.ndarray:
    """The 3 by 3 matrix of the alignment CSV, as published."""
    matrix = np.loadtxt(io.StringIO(text), delimiter=",", ndmin=2)
    if matrix.shape != (3, 3):
        raise ValueError(f"the alignment has shape {matrix.shape}, not 3 by 3")
    return matrix


def write_he(writer: ElementWriter, he_url: str, alignment_url: str, pixel_size: float, *, concurrency: int) -> None:
    """Write every level of the H&E image, channel first, as stored; ``pixel_size`` is the morphology pixel.

    The alignment maps H&E pixels to morphology pixels, so the transform into ``global`` applies it, then ``pixel_size``.
    """
    wrap = writer.counter.wrap if writer.counter else None
    alignment = read_alignment(reader(as_url(alignment_url), wrap=wrap).read().decode())
    pyramid = open_tiff(reader(as_url(he_url), wrap=wrap)).series[0].levels
    shapes = [(page.samplesperpixel, page.imagelength, page.imagewidth) for page in (level.pages[0] for level in pyramid)]
    # The two steps chain through the system of morphology pixels.
    transform = sequence(
        [affine(alignment, ("x", "y"), ("x", "y"), system=MORPHOLOGY_PIXEL, unit="pixel"),
         scale([pixel_size] * 2, ("x", "y"), input_system=MORPHOLOGY_PIXEL, input_unit="pixel", unit=UNIT)],
        ("c", "y", "x"), unit=UNIT)

    def write_level(group, level):
        page = pyramid[level].pages[0]
        array = create_level(group, f"s{level}", shapes[level], page.dtype, dims=("c", "y", "x"),
                             chunk=(page.tilelength, page.tilewidth))
        write_page(array, page, (), concurrency=concurrency)

    def attrs(held):
        # The matrix as published stays beside the transform that applies it.
        return {**raster_attrs(level_ome("he_image", ("c", "y", "x"), shapes, held, rgb_omero()), transform),
                "alignment": {"matrix": alignment.tolist(), "axes": ["x", "y"],
                              "from": "he_image pixels", "to": "morphology pixels"}}

    write_levels(writer, "images/he_image", len(shapes), None, write_level, attrs)


def table_properties(sample, layouts: str, repo: str, *, element: bool = True, **extra) -> dict[str, str]:
    """Attribution on every table, so a table read on its own still attributes.

    ``element`` is false for a table that no element references; the root attribute pins it.
    """
    referenced = (f"The uint8 arrays of the Icechunk repo {repo} reference this table's data files." if element
                  else f"The root attribute iceberg_tables of the Icechunk repo {repo} pins this table's snapshot.")
    properties = {
        "license": json.dumps(LICENSE),
        "citation": citation_text(sample),
        "source": json.dumps(source_objects(layouts, sample)),
        "referenced_by": referenced,
    }
    return properties | {k: v if isinstance(v, str) else json.dumps(v) for k, v in extra.items()}


def table_attr(tables: TableWriter, name: str, ref, *, geo=None, index_column: str | None = None) -> dict:
    """The ``iceberg_table`` attribute that references table ``name`` at ``ref``, for an element or the root."""
    return iceberg_table_attr(ref, catalog=tables.catalog_name, org=tables.org, namespace=tables.namespace,
                              table=name, index_column=index_column or tables.index_column,
                              geometry_column="geometry" if geo else None)


def table_specs(properties: dict, methods: list[str]) -> dict[str, dict]:
    """Each Iceberg table's schema, partitioning and properties, known before any element is written."""
    polygons = {
        "schema": POLYGON_SCHEMA,
        "properties": {**properties, "geo": json.dumps(GEO_POLYGONS), "method_names": json.dumps(methods)},
    }
    return {
        "nucleus_boundaries": polygons,
        "cell_boundaries": polygons,
        "transcripts": {
            "schema": TRANSCRIPT_SCHEMA,
            "partition_by": "grid_tile",
            "properties": {**properties, "quality_score": "Widened from the source's float16 to float32."},
        },
    }


def write_boundaries(writer, bundle, tables: TableWriter, name: str, set_index: int, *, replace=False) -> None:
    """Write polygon set ``set_index`` as the table and the element ``name``."""
    zf = bundle.member_zip(CELLS)
    path = f"polygon_sets/{set_index}/"
    total = member_bytes(zf, path) + member_bytes(zf, "cell_id/")
    with writer.unit(f"shapes/{name}", f"Write shapes/{name}", total=total) as (root, _):
        cell_id = read_array(zf, "cell_id")
        table = polygon_table(zf, set_index, xenium_cell_id(cell_id[:, 0], cell_id[:, 1]))
        ref = tables.write(name, table, geo=GEO_POLYGONS, replace=replace)
        attr = table_attr(tables, name, ref, geo=GEO_POLYGONS)
        group = root.require_group("shapes").create_group(name, overwrite=replace)
        group.attrs.update({**shapes_attrs("POLYGON", scale([1.0, 1.0], ("x", "y"), unit=UNIT)), "iceberg_table": attr})
        declare_copy(group, "shapes.parquet", ref.files)


def write_extra_tables(writer, bundle, tables: TableWriter, sample, layouts: str, repo: str) -> None:
    """Write the codewords and the two differential expression tables, then pin them in the root attribute.

    A branch that holds the attribute has every table, so a rerun skips the unit.
    """
    if writer.has_attribute(ICEBERG_TABLES):
        print(f"{ICEBERG_TABLES}: on branch {writer.branch} already, skipped", flush=True)
        return
    properties = table_properties(sample, layouts, repo, element=False)
    built = {"codewords": (codewords_table(bundle.member_zip(TRANSCRIPTS)), "codeword_index")}
    for name, member in DIFFEXP_TABLES.items():
        with bundle.member_file(member) as f:
            built[name] = (diffexp_table(f.read().decode()), "Feature ID")
    with writer.unit(ICEBERG_TABLES, "Write the codewords and differential expression tables") as (root, _):
        pins = {}
        for name, (table, index_column) in built.items():
            tables.require(name, schema=table.schema, properties=properties)
            ref = tables.write(name, table, index_column=index_column)
            pins[name] = table_attr(tables, name, ref, index_column=index_column)
        root.attrs[ICEBERG_TABLES] = pins


def write_transcripts(
    writer, bundle, tables: TableWriter, *, tiles: Sequence[str] | None = None, replace=False,
    scratch: Path | None = None,
) -> None:
    """Write ``tiles`` of the transcripts, or every tile when ``None``, one table snapshot per tile.

    ``instance`` counts the rows of ``tiles`` in the order given. A tile that the table holds is skipped;
    a held tile with another row count raises. With ``replace`` the first tile replaces the table's files.
    """
    zf = bundle.member_zip(TRANSCRIPTS)
    grid = json.loads(zf.read("grid/.zattrs"))
    counts = dict(zip(grid["grid_keys"], grid["grid_number_objects"], strict=True))
    if unknown := [t for t in tiles or () if t not in counts]:
        raise ValueError(f"the source has no tile {', '.join(unknown)}")
    keys = list(counts if tiles is None else tiles)
    plan = tile_order({"grid_keys": keys, "grid_number_objects": [counts[t] for t in keys]})
    total = sum(member_bytes(zf, f"grid/{t}/") for t, _ in plan) + member_bytes(zf, "codeword_category/")
    with writer.unit("points/transcripts", "Write points/transcripts", total=total) as (root, _), \
            contextlib.ExitStack() as stack:
        directory = scratch or Path(stack.enter_context(tempfile.TemporaryDirectory()))
        codewords, field_ids = codewords_table(zf), tables.field_ids("transcripts")
        held = {} if replace else tables.partitions("transcripts")
        if outside := sorted(set(held) - set(keys)):
            raise ValueError(f"the table holds tiles outside the plan: {', '.join(outside)}")
        held_ranges = {} if replace or not held else tables.partition_ranges("transcripts", "instance")
        all_rows, done, written, started = sum(counts[t] for t, _ in plan), 0, 0, time.monotonic()
        replacing = replace
        for index, (tile, first_instance) in enumerate(plan):
            rows = counts[tile]
            if tile in held and held[tile] != rows:
                raise ValueError(f"tile {tile}: the table holds {held[tile]} rows, the source has {rows}")
            if tile in held and held_ranges[tile] != (first_instance, first_instance + rows - 1):
                raise ValueError(f"tile {tile}: the table holds instances {held_ranges[tile][0]}..{held_ranges[tile][1]}, "
                                 f"expected {first_instance}..{first_instance + rows - 1}")
            if tile in held or rows == 0:
                print(f"tile {tile}: {'in the table already' if rows else 'no rows'}, skipped", flush=True)
                done += rows
                continue
            path = directory / f"tile-{index}.parquet"
            try:
                write_tile(zf, tile, codewords, path, first_instance=first_instance, field_ids=field_ids)
                tables.write_parts("transcripts", [path], replace=replacing)
            finally:
                path.unlink(missing_ok=True)
            replacing = False
            done += rows
            written += rows
            print(f"tile {tile}: {rows:,} rows, {done:,} of {all_rows:,}, "
                  f"{written / (time.monotonic() - started):,.0f} rows/s", flush=True)
        ref = tables.ref("transcripts", partition_order=[t for t, _ in plan if counts[t]])
        attr = table_attr(tables, "transcripts", ref)
        group = root.require_group("points").create_group("transcripts", overwrite=replace)
        transform = scale([1.0, 1.0, 1.0], ("x", "y", "z"), unit=UNIT)
        group.attrs.update({**points_attrs(("x", "y", "z"), transform, instance_key="cell_id",
                                           feature_key="feature_name"), "iceberg_table": attr})
        declare_copy(group, "points.parquet", ref.files)


def write_root(writer, bundle, sample, layouts: str, experiment: dict, *, missing: list, fixture: str | None = None) -> None:
    metrics_name = next(n for n in bundle.names if n.startswith("metrics_summary_"))
    with writer.unit("root attributes", "Write root attributes") as (root, _):
        with bundle.member_file(metrics_name) as f:
            metrics = parse_metrics(f.read().decode())
        root.attrs.update(root_attrs(sample, layouts, experiment, metrics, missing=missing, fixture=fixture))


def check_bundle_size(bundle: Bundle, sample, layouts: str) -> None:
    recorded = source_objects(layouts, sample)[0]
    size = bundle.file.seek(0, os.SEEK_END)
    if size != recorded["size"]:
        raise ValueError(f"{recorded['url']} is {size} bytes; LAYOUTS.md records {recorded['size']}")


def rewrite_elements(text: str) -> set[str]:
    """The comma-separated elements of ``--rewrite``; each must be in :data:`REWRITABLE`."""
    elements = set(text.split(","))
    if unknown := elements - set(REWRITABLE):
        raise ValueError(f"cannot rewrite {', '.join(sorted(unknown))}; rewritable: {', '.join(REWRITABLE)}")
    return elements


def select_elements(text: str | None, available: Sequence[str]) -> list[str]:
    """The comma-separated elements of ``--elements`` in the order of ``available``, or all of them without ``text``."""
    if not text:
        return list(available)
    wanted = set(text.split(","))
    if unknown := wanted - set(available):
        raise ValueError(f"unknown elements: {', '.join(sorted(unknown))}; known: {', '.join(available)}")
    return [e for e in available if e in wanted]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", choices=sorted(SAMPLES), required=True)
    parser.add_argument("--repo", required=True, help="a local directory, or arraylake://<org>/<repo>")
    parser.add_argument("--catalog", required=True,
                        help="a local directory for the catalog and its warehouse, or arraylake:<org>")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--mode", choices=sorted(PROTECTED), default="fixture",
                        help="fixture writes one level, one tile and the polygons; full writes every element")
    parser.add_argument("--branch", default="fixture")
    parser.add_argument("--concurrency", type=int, default=1, help="chunk writes that run at once inside an image or mask")
    parser.add_argument("--he-image", help="the H&E OME-TIFF, a URL or a local path; by default 10x's object")
    parser.add_argument("--he-alignment", help="the H&E alignment CSV, a URL or a local path; by default 10x's object")
    parser.add_argument("--elements", help="write only these elements, comma-separated, of the mode's elements")
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    parser.add_argument("--rewrite", type=rewrite_elements, default=set(),
                        help=f"write only these held elements again, comma-separated, of {', '.join(REWRITABLE)}")
    args = parser.parse_args()
    if args.rewrite and args.branch == "main":
        raise SystemExit("--rewrite never writes to main")
    if args.branch in PROTECTED[args.mode]:
        raise SystemExit(f"--mode {args.mode} never writes to the branch {args.branch}")
    if args.rewrite and args.elements:
        raise SystemExit("--elements does not apply to --rewrite; --rewrite names its own elements")
    if args.rewrite and args.mode == "full":
        raise SystemExit("--rewrite is an option of --mode fixture")
    try:
        selected = select_elements(args.elements, ELEMENTS[args.mode])
    except ValueError as e:
        raise SystemExit(str(e))
    on_arraylake = args.catalog.startswith("arraylake:")
    if on_arraylake != args.repo.startswith(ARRAYLAKE):
        raise SystemExit("--repo and --catalog must both be local or both be on Arraylake.")

    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    sample, layouts = SAMPLES[args.sample], LAYOUTS.read_text()
    bundle, counter = open_bundle(args.bundle or sample.url("_outs.zip", S3))
    if not args.bundle:
        check_bundle_size(bundle, sample, layouts)
    experiment = bundle.experiment()
    pixel_size = experiment["pixel_size"]

    if on_arraylake:
        from arraylake import Client

        org = args.catalog.removeprefix("arraylake:")
        tables = TableWriter(Client().get_iceberg(org), args.namespace, catalog_name="arraylake", org=org)
    else:
        tables = TableWriter(local_catalog(Path(args.catalog)), args.namespace, catalog_name="local", org="local")
    methods = json.loads(bundle.member_zip(CELLS).read(".zattrs"))["segmentation_methods"]
    specs = table_specs(table_properties(sample, layouts, sample.repo), methods)
    # The tables exist before any element, so a repo without a container over their files fails here.
    for name in FIXTURE_TABLES:
        tables.require(name, **specs[name])
    prefixes = [tables.data_prefix(name) for name in FIXTURE_TABLES]
    if on_arraylake:
        repo = arraylake_repository(args.repo, source_prefixes=prefixes)
    else:
        repo = create_repo(args.repo, [], prefixes=prefixes, open_existing=True)
    if args.branch not in repo.list_branches():
        repo.create_branch(args.branch, repo.lookup_branch("main"))
    writer = ElementWriter(repo, args.branch, counter=counter, protected=PROTECTED[args.mode])

    started = time.monotonic()
    if args.rewrite:
        # A rewrite touches only the named elements; the tables keep their UUIDs.
        if "tables/table:attrs" in args.rewrite:
            write_table_index(writer)
        if "shapes/nucleus_boundaries" in args.rewrite:
            write_boundaries(writer, bundle, tables, "nucleus_boundaries", NUCLEUS_SET, replace=True)
        if "points/transcripts" in args.rewrite:
            write_transcripts(writer, bundle, tables, tiles=[FIXTURE_TILE], replace=True)
        print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
        print(f"rewritten: {', '.join(sorted(args.rewrite))}")
        return
    n = args.concurrency
    he_image = args.he_image or sample.url(HE_IMAGE, S3)
    he_alignment = args.he_alignment or sample.url(HE_ALIGNMENT, S3)
    full = args.mode == "full"
    steps = {
        "tables/table": unless_held(writer, "tables/table", lambda: write_table(writer, bundle)),
        **{f"labels/{name}": partial(write_labels, writer, bundle, pixel_size, names=[name], concurrency=n)
           for name in MASKS},
        **{f"shapes/{name}": unless_held(writer, f"shapes/{name}",
                                         partial(write_boundaries, writer, bundle, tables, name, index))
           for name, index in (("nucleus_boundaries", NUCLEUS_SET), ("cell_boundaries", CELL_SET))},
        "points/transcripts": unless_held(
            writer, "points/transcripts",
            lambda: write_transcripts(writer, bundle, tables, tiles=None if full else [FIXTURE_TILE])),
        ICEBERG_TABLES: lambda: write_extra_tables(writer, bundle, tables, sample, layouts, sample.repo),
        "images/morphology": lambda: write_morphology(writer, bundle, pixel_size, levels=None if full else [FIXTURE_LEVEL],
                                                      concurrency=n),
        "images/morphology_3d": lambda: write_morphology_3d(writer, bundle, pixel_size, concurrency=n),
        "images/he_image": lambda: write_he(writer, he_image, he_alignment, pixel_size, concurrency=n),
        "root": lambda: write_root(writer, bundle, sample, layouts, experiment,
                                   missing=MISSING_FROM_FULL if full else MISSING_FROM_FIXTURE,
                                   fixture=None if full else FIXTURE),
    }
    for element in selected:
        steps[element]()
    print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
    print(f"{args.mode} written: {', '.join(selected)}")


if __name__ == "__main__":
    main()
