"""Write an Atera sample into an Icechunk repo and an Iceberg catalog, local or on Arraylake.

The script has two modes. ``fixture`` writes the fixture of PLAN.md, "Steps, scope and risk", step 1.
``full`` writes every element of PLAN.md, "The tree", except the ones that ``MISSING_FROM_FULL`` lists.
Each mode writes onto a branch, one commit per element, or per level for an image.
The bundle is read in place by range requests.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd
import zarr
from anndata.io import write_elem
from minus80.core.elements import COMPLETE, ElementWriter
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo, open_repository
from minus80.core.progress import MIB
from minus80.core.registry import as_url
from minus80.core.remote import reader
from minus80.core.pyramid import channel_names, create_level, physical_sizes, write_page, write_stack
from minus80.core.tables import HeldFilesError, TableWriter, declare_copy, iceberg_table_attr, local_catalog, schema_changes
from minus80.core.zarr_copy import copy_chunks, copy_group, create_copy, read_array
from minus80.ome import channel_omero, image_attrs, label_attrs, rgb_omero
from minus80.ome.spatialdata import (
    affine, points_attrs, raster_attrs, scale, sequence, shapes_attrs, translation, write_table_keys,
)

from bundle import (
    CELLS, DIFFEXP_AZIMUTH, DIFFEXP_GRAPHCLUST, MATRIX, TRANSCRIPTS, Bundle, he_readers, open_bundle, open_tiff,
    read_alignment,
)
from check_bundle import differences, summary
from facts import bundle_facts, load_record
from polygons import GEO_POLYGONS, POLYGON_SCHEMA, polygon_table
from qc import QC_NAMES, QcRaster, bundle_rasters
from root_attrs import (
    LAYOUTS, LICENSE, ROOT_KEYS, citation_text, element_kind, parse_metrics, root_attrs, root_changes, source_objects,
)
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, S3, SAMPLES
from tables_extra import codewords_table, diffexp_table
from transcripts import TRANSCRIPT_SCHEMA, tile_order, write_tile, xenium_cell_id

#: Label element, then the cells store's mask array; PLAN.md, judgement call 1.
MASKS = {"nucleus_labels": "masks/0", "cell_labels": "masks/1"}
NUCLEUS_SET, CELL_SET = 0, 1
#: The root attribute that pins the tables that annotate no element, each as an ``iceberg_table`` attribute does.
ICEBERG_TABLES = "iceberg_tables"
#: The Iceberg tables that an element holds, in both modes.
ELEMENT_TABLES = ("nucleus_boundaries", "cell_boundaries", "transcripts")
#: Each differential expression table with its bundle member.
DIFFEXP_TABLES = {"diffexp_azimuth": DIFFEXP_AZIMUTH, "diffexp_graphclust": DIFFEXP_GRAPHCLUST}
#: The tables that the root attribute ``iceberg_tables`` pins.
EXTRA_TABLES = ("codewords", *DIFFEXP_TABLES)
FIXTURE_LEVEL = 6
FIXTURE_TILE = "0,0"
REGION_KEY, INSTANCE_KEY = "region", "instance"
#: The one element that the table annotates; PLAN.md, "The tables".
TABLE_REGION = "cell_boundaries"
#: The unit of the global coordinate system; PLAN.md, judgement call 4.
UNIT = "micrometer"
#: The coordinate system between the H&E alignment and the scale into ``global``.
MORPHOLOGY_PIXEL = "morphology_pixel"
#: The coordinate system between the scale and the translation of a QC raster.
QC_SCALED = "qc_scaled"
QC_PREFIX = "images/qc_"
#: The branches that each mode refuses to write.
PROTECTED = {"fixture": ("main",), "full": ("fixture",)}
#: The elements each mode writes, in order. In full mode the catalog and QC elements come first, so a fault
#: shows before the hours of image writes. The root attributes come last, as they describe the branch.
ELEMENTS = {
    "fixture": ("tables/table", *(f"labels/{n}" for n in MASKS), "images/morphology", "shapes/nucleus_boundaries",
                "shapes/cell_boundaries", "points/transcripts", "root"),
    "full": ("tables/table", *(f"labels/{n}" for n in MASKS), "shapes/nucleus_boundaries", "shapes/cell_boundaries",
             "points/transcripts", ICEBERG_TABLES, *(f"images/qc_{n}" for n in QC_NAMES), "images/morphology",
             "images/morphology_3d", "images/he_image", "root"),
}
#: The tables ``--replace-tables`` can replace in place: each table the full mode writes.
REPLACEABLE = (*ELEMENT_TABLES, *EXTRA_TABLES)
#: The element whose step writes each replaceable table's rows.
REPLACE_ELEMENTS = {"transcripts": "points/transcripts", "nucleus_boundaries": "shapes/nucleus_boundaries",
                    "cell_boundaries": "shapes/cell_boundaries", **dict.fromkeys(EXTRA_TABLES, ICEBERG_TABLES)}
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
    *NOT_WRITTEN,
]


def catalog_org(location: str) -> str | None:
    """The org of the catalog location ``arraylake://<org>``, or ``None`` for a local directory."""
    return location.removeprefix(ARRAYLAKE) if location.startswith(ARRAYLAKE) else None


def require_same_place(repo: str, catalog: str) -> None:
    """Exit unless the repo and the catalog are both local or both ``arraylake://`` locations."""
    if catalog.startswith("arraylake:") and not catalog.startswith(ARRAYLAKE):
        raise SystemExit(f"--catalog {catalog} is not a location; use arraylake://<org>.")
    if (catalog_org(catalog) is not None) != repo.startswith(ARRAYLAKE):
        raise SystemExit("--repo and --catalog must both be local or both be on Arraylake.")


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


def write_table(writer: ElementWriter, bundle: Bundle, *, concurrency: int) -> None:
    zf = bundle.member_zip(MATRIX)
    with writer.unit("tables/table", "Write tables/table", total=member_bytes(zf, "")) as (root, _):
        group = root.require_group("tables").create_group("table")
        copy_group(zf, "", group, concurrency=concurrency)
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
            # The unit commits whole, so a mask on the branch is a complete mask.
            group.attrs[COMPLETE] = True


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
    files = bundle.morphology_2d()
    tiffs = [bundle.tiff(name) for name in files]
    for name, tif in zip(files, tiffs, strict=True):
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
    stack = bundle.morphology_3d()
    tif = bundle.tiff(stack)
    z_step = file_pixel_sizes(tif, pixel_size, stack).get("z")
    if z_step is None:
        raise ValueError(f"{stack}: the OME-XML has no PhysicalSizeZ")
    # One series holds every plane: its level k is a ``(z, y, x)`` array with one page for each plane.
    pyramid = tif.series[0].levels
    shapes = [(1, len(level.pages), *level.shape[1:]) for level in pyramid]
    names = channel_names(tif.pages.first.description)
    omero = channel_omero([{"label": n} for n in names], max_value=np.iinfo(pyramid[0].dtype).max)

    def write_level(group, level):
        pages = list(pyramid[level].pages)
        array = create_level(group, f"s{level}", shapes[level], pages[0].dtype, dims=("c", "z", "y", "x"),
                             chunk=(pages[0].tilelength, pages[0].tilewidth), shard_planes=len(pages))
        write_stack(array, pages, (0,), concurrency=concurrency, decoders=min(len(pages), os.cpu_count() or 1))

    write_levels(writer, "images/morphology_3d", len(shapes), levels, write_level,
                 lambda held: raster_attrs(level_ome("morphology_3d", ("c", "z", "y", "x"), shapes, held, omero),
                                           level_scale(("c", "z", "y", "x"), shapes, held,
                                                       {"c": 1.0, "z": z_step, "y": pixel_size, "x": pixel_size})))


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


def write_qc_raster(writer: ElementWriter, raster: QcRaster) -> None:
    """Write ``raster`` as ``images/qc_<name>``, channel first, one chunk.

    Pixel ``p`` of an axis starts at ``origin + p * bin_size`` in micrometres, as in the other image elements.
    """
    path, axes, shape = f"images/qc_{raster.name}", ("c", "y", "x"), (1, *raster.array.shape)
    transform = sequence(
        [scale([1.0, raster.bin_size, raster.bin_size], axes, system=QC_SCALED, unit=UNIT),
         translation([0.0, raster.origin[1], raster.origin[0]], axes, input_system=QC_SCALED, input_unit=UNIT,
                     unit=UNIT)],
        axes, unit=UNIT)
    # The display window spans the finite values: the dtype's range would show a map of small counts as black.
    top = raster.array[np.isfinite(raster.array)].max()
    omero = channel_omero([{"label": raster.label}], max_value=max(math.ceil(top), 1))

    def write_level(group, level):
        array = create_level(group, f"s{level}", shape, raster.array.dtype, dims=axes, chunk=shape[1:])
        array[0] = raster.array

    write_levels(writer, path, 1, None, write_level,
                 lambda held: raster_attrs(level_ome(f"qc_{raster.name}", axes, [shape], held, omero), transform))


def write_qc(writer: ElementWriter, rasters: dict[str, QcRaster], names: Sequence[str] | None = None) -> None:
    """Write the QC rasters ``names`` of ``rasters``, all by default; skip a raster that the branch holds.

    ``rasters`` are the decoded rasters of the analysis summary.
    """
    if names is not None:
        # A raster that the branch holds needs no entry in ``rasters``.
        names = [n for n in names if not skip_held(writer, f"images/qc_{n}")]
        if not names:
            return
    for name in names or rasters:
        if name not in rasters:
            raise ValueError(f"the analysis summary has no QC raster {name}; it has {', '.join(rasters)}")
        if not skip_held(writer, f"images/qc_{name}"):
            write_qc_raster(writer, rasters[name])


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


def require_table_listing(tables: TableWriter, name: str, spec: dict, evolve: bool):
    """Require table ``name``. With ``evolve`` it takes the columns of ``spec``, with a line that lists the change."""
    identifier, changes = f"{tables.namespace}.{name}", ([], [])
    if evolve and tables.catalog.table_exists(identifier):
        changes = schema_changes(tables.catalog.load_table(identifier), spec["schema"])
    table = tables.require(name, **spec, evolve=evolve)
    if any(changes):
        print(f"{name}: columns added: {', '.join(changes[0]) or 'none'}; "
              f"columns deleted: {', '.join(changes[1]) or 'none'}", flush=True)
    return table


def require_tables(tables: TableWriter, specs: dict[str, dict], replace: set[str]) -> None:
    """Require each of ``ELEMENT_TABLES``; a table in ``replace`` takes the columns of its spec."""
    for name in ELEMENT_TABLES:
        require_table_listing(tables, name, specs[name], name in replace)


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


def write_extra_tables(writer, bundle, tables: TableWriter, sample, layouts: str, repo: str, replace=frozenset()) -> None:
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
            require_table_listing(tables, name, {"schema": table.schema, "properties": properties}, name in replace)
            ref = tables.write(name, table, index_column=index_column, replace=name in replace)
            pins[name] = table_attr(tables, name, ref, index_column=index_column)
        root.attrs[ICEBERG_TABLES] = pins


def _span(rows: int, low: int, high: int) -> str:
    """The row count and ``instance`` range of a tile as text; an empty tile has no range."""
    return f"{rows} rows, instances {low}..{high}" if rows else "no rows"


def held_tile_faults(plan: Sequence[tuple[str, int]], counts: dict, held: dict, held_ranges: dict) -> dict[str, str]:
    """Why each held tile of ``plan`` fails: another row count, or another ``instance`` range than its place in the order."""
    faults = {}
    for tile, first_instance in plan:
        rows = counts[tile]
        expected = (first_instance, first_instance + rows - 1)
        if tile in held and (held[tile] != rows or held_ranges[tile] != expected):
            faults[tile] = f"the table holds {_span(held[tile], *held_ranges[tile])}; expected {_span(rows, *expected)}"
    return faults


def write_transcripts(
    writer, bundle, tables: TableWriter, *, tiles: Sequence[str] | None = None, replace=False,
    replace_held=False, scratch: Path | None = None,
) -> None:
    """Write ``tiles`` of the transcripts, or every tile when ``None``, one table snapshot per tile.

    ``instance`` counts the rows of ``tiles`` in the order given. A tile that the table holds is skipped;
    a held tile with another row count or ``instance`` range raises, and so does a held tile outside the plan.
    That check covers every held tile and runs before the first write, so a raise leaves the table as it was.
    With ``replace`` the first tile replaces the table's files.
    With ``replace_held`` a failing held tile is replaced in the one snapshot that adds its file,
    and one snapshot first removes the tiles outside the plan. The files stay in storage.
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
        outside = sorted(set(held) - set(keys))
        held_ranges = {} if replace or not held else tables.partition_ranges("transcripts", "instance")
        faults = held_tile_faults(plan, counts, held, held_ranges)
        if (outside or faults) and not replace_held:
            raise ValueError("; ".join([*(f"tile {t}: {m}" for t, m in faults.items()),
                                        *([f"the table holds tiles outside the plan: {', '.join(outside)}"] if outside else [])]))
        # A failing tile with no rows has nothing to write, so it only goes.
        if replace_held and (dropped := [*outside, *(t for t in faults if not counts[t])]):
            tables.remove_partitions("transcripts", dropped)
            print(f"tiles {', '.join(dropped)}: removed from the table", flush=True)
            held = {t: n for t, n in held.items() if t not in dropped}
        all_rows, done, written, started = sum(counts[t] for t, _ in plan), 0, 0, time.monotonic()
        replacing = replace
        for index, (tile, first_instance) in enumerate(plan):
            rows = counts[tile]
            if (tile in held and tile not in faults) or rows == 0:
                print(f"tile {tile}: {'in the table already' if rows else 'no rows'}, skipped", flush=True)
                done += rows
                continue
            if tile in held:
                print(f"tile {tile}: {faults[tile]}, written again", flush=True)
            path = directory / f"tile-{index}.parquet"
            try:
                write_tile(zf, tile, codewords, path, first_instance=first_instance, field_ids=field_ids)
                tables.write_parts("transcripts", [path], replace=replacing, replace_partitions=[tile] if tile in held else ())
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


def build_root(
    bundle: Bundle, sample, layouts: str, experiment: dict, mode: str, *, missing: list, fixture: str | None = None,
) -> dict:
    """The root attributes of ``sample`` from the bundle's metrics and ``experiment``; the description covers ``mode``."""
    with bundle.member_file(bundle.metrics()) as f:
        metrics = parse_metrics(f.read().decode())
    kinds = {element_kind(e) for e in ELEMENTS[mode]}
    return root_attrs(sample, layouts, experiment, metrics, kinds=kinds, missing=missing, fixture=fixture)


def write_root(writer, root_attributes: dict, keys: Collection[str] | None = None) -> None:
    """Write the ``keys`` of ``root_attributes``, all by default, after one line that lists each key that changes.

    A key outside ``keys`` that changes raises before the write.
    """
    with writer.unit("root attributes", "Write root attributes") as (root, _):
        changes = root_changes(root.attrs.asdict(), root_attributes)
        print(f"root attributes: changes {', '.join(changes) or 'none'}", flush=True)
        if keys is not None and (unnamed := [k for k in changes if k not in keys]):
            raise ValueError(f"root attributes: --root-keys does not name these keys that change: {', '.join(unnamed)}")
        root.attrs.update({k: v for k, v in root_attributes.items() if keys is None or k in keys})


@dataclass(frozen=True)
class Prepared:
    """The facts of a bundle that the steps of the full mode need, all read before the first write."""

    experiment: dict
    root: dict
    methods: list[str]
    qc: dict[str, QcRaster]


#: The ingest reads the stores at their steps, and the ``members`` group pins the size of each store member.
#: ``check_bundle.py`` compares the ``stores`` group before the download.
INGEST_SKIPS = ("stores",)


def prepare(
    bundle: Bundle, sample, layouts: str, open_he: Callable, alignment_text: Callable, *, record: dict,
) -> Prepared:
    """Compare ``bundle`` with ``record``, then read every fact that a later step needs.

    ``ValueError`` gives the difference lines, or the fact that the bundle lacks. The check reads no pixel and no table row.
    """
    decoded = {}
    found = bundle_facts(bundle, open_he, alignment_text, INGEST_SKIPS, decoded)
    if lines := differences(record, found, INGEST_SKIPS):
        raise ValueError("\n".join(lines))
    print(summary(0, INGEST_SKIPS), flush=True)
    # Each member pattern that a step opens must match its count of files.
    bundle.morphology_2d(), bundle.morphology_3d(), bundle.metrics(), bundle.report()
    experiment = bundle.experiment()
    try:
        # The image and label steps read this key.
        experiment["pixel_size"]
        root = build_root(bundle, sample, layouts, experiment, "full", missing=MISSING_FROM_FULL)
    except KeyError as error:
        raise ValueError(f"experiment.spatial has no key {error.args[0]!r}, which the ingest needs") from None
    return Prepared(experiment=experiment, root=root, methods=found["polygons"]["method_names"],
                    qc=decoded or bundle_rasters(bundle))


def check_bundle_size(bundle: Bundle, sample, layouts: str) -> None:
    bundles = [o for o in source_objects(layouts, sample) if o["url"].endswith(BUNDLE)]
    if len(bundles) != 1:
        raise ValueError(f"LAYOUTS.md records {len(bundles)} objects of {sample.repo} that end in {BUNDLE}, not 1")
    (recorded,) = bundles
    size = bundle.file.seek(0, os.SEEK_END)
    if size != recorded["size"]:
        raise ValueError(f"{recorded['url']} is {size} bytes; LAYOUTS.md records {recorded['size']}")


def rewrite_elements(text: str) -> set[str]:
    """The comma-separated elements of ``--rewrite``; each must be in :data:`REWRITABLE`."""
    elements = set(text.split(","))
    if unknown := elements - set(REWRITABLE):
        raise ValueError(f"cannot rewrite {', '.join(sorted(unknown))}; rewritable: {', '.join(REWRITABLE)}")
    return elements


def replace_table_names(text: str) -> set[str]:
    """The comma-separated tables of ``--replace-tables``; each must be in :data:`REPLACEABLE`."""
    names = set(text.split(","))
    if unknown := names - set(REPLACEABLE):
        raise ValueError(f"cannot replace {', '.join(sorted(unknown))}; replaceable: {', '.join(REPLACEABLE)}")
    return names


def check_replace_elements(replace: set[str], selected: Sequence[str]) -> None:
    """Raise ``ValueError`` when a table of ``replace`` has no element in ``selected``.

    A schema change with no row write would leave the table with the schema and the earlier rows.
    """
    if lacking := sorted(t for t in replace if REPLACE_ELEMENTS[t] not in selected):
        raise ValueError(f"--replace-tables {', '.join(lacking)} needs the element "
                         f"{', '.join(REPLACE_ELEMENTS[t] for t in lacking)} in the run")


def root_key_names(text: str, selected: Sequence[str]) -> set[str]:
    """The comma-separated keys of ``--root-keys``; each must be in :data:`ROOT_KEYS`, and ``root`` the only element.

    A run with another element would write it before the root step can refuse.
    """
    keys = set(text.split(","))
    if unknown := keys - set(ROOT_KEYS):
        raise ValueError(f"cannot write the root keys {', '.join(sorted(unknown))}; known: {', '.join(ROOT_KEYS)}")
    if list(selected) != ["root"]:
        raise ValueError("--root-keys needs --elements root alone")
    return keys


def select_elements(text: str | None, available: Sequence[str]) -> list[str]:
    """The comma-separated elements of ``--elements`` in the order of ``available``, or all of them without ``text``."""
    if not text:
        return list(available)
    wanted = set(text.split(","))
    if unknown := wanted - set(available):
        raise ValueError(f"unknown elements: {', '.join(sorted(unknown))}; known: {', '.join(available)}")
    return [e for e in available if e in wanted]


def _preflight_table(catalog, namespace: str, name: str, specs: dict, replace: set[str], problems: list[str]) -> str | None:
    """Print the plan for table ``name`` and add a problem if nothing covers its difference; its data prefix if it exists."""
    identifier = f"{namespace}.{name}"
    if not catalog.table_exists(identifier):
        print(f"{name}: absent; the ingest creates it", flush=True)
        return None
    table = catalog.load_table(identifier)
    tasks = list(table.scan().plan_files())
    held = f"{len(tasks)} files, {sum(t.file.record_count for t in tasks)} rows held"
    # A single-write table refuses other content without the flag; an equal rerun is a no-op.
    rerun = f"an equal-content rerun is a no-op, and different content needs --replace-tables {name}"
    if name not in specs:
        print(f"{name}: {held}; its schema is checked at its step; {rerun}", flush=True)
        return f"{table.location()}/data/"
    wanted = [specs[name]["partition_by"]] if "partition_by" in specs[name] else []
    present = [table.schema().find_column_name(f.source_id) for f in table.spec().fields]
    schema = specs[name]["schema"]
    lacking = [f.name for f in schema if not f.nullable and f.name not in {f.name for f in table.schema().fields}]
    if present != wanted:
        problems.append(f"{name}: partitioned by {present}, not {wanted}")
    elif lacking:
        problems.append(f"{name}: has no column {', '.join(lacking)}")
    else:
        try:
            added, deleted = schema_changes(table, schema)
        except ValueError as e:
            problems.append(str(e))
            return f"{table.location()}/data/"
        if (added or deleted) and name not in replace:
            problems.append(f"{name}: differs from the wanted schema, which adds {', '.join(added) or 'no column'} and "
                            f"deletes {', '.join(deleted) or 'no column'}; nothing covers it without --replace-tables")
        else:
            change = f"add {', '.join(added) or 'nothing'}; delete {', '.join(deleted) or 'nothing'}"
            plan = [change if added or deleted else "matches", *(["replace rows"] if name in replace else [])]
            if name != "transcripts" and tasks and name not in replace:
                plan += [held, rerun]
            print(f"{name}: {'; '.join(plan)}", flush=True)
            if name == "transcripts":
                print(f"{name}: partitioned by {', '.join(present)}", flush=True)
                print(f"{name}: {len(tasks)} files held; held tiles are checked against the source at the transcripts step",
                      flush=True)
    return f"{table.location()}/data/"


def _preflight_tables(catalog, namespace: str, replace: set[str], problems: list[str]) -> list[str]:
    """Run :func:`_preflight_table` for each table of the full mode; a failure is one problem with the exception type only.

    Returns the data prefixes of the tables that exist.
    """
    specs, prefixes = table_specs({}, []), []
    for name in REPLACEABLE:
        try:
            prefix = _preflight_table(catalog, namespace, name, specs, replace, problems)
        except Exception as e:  # The text of a storage error can hold a location.
            problems.append(f"{name}: does not load ({type(e).__name__})")
            continue
        if prefix:
            prefixes.append(prefix)
    return prefixes


def _open_existing_repo(repo: str, prefixes: list[str]):
    """The repo, or ``None`` for a local directory that is absent. Any other failure to open raises."""
    if repo.startswith(ARRAYLAKE):
        return arraylake_repository(repo, source_prefixes=prefixes)
    return open_repository(repo) if Path(repo).exists() else None


def report_held_replacements(writer, replace: set[str]) -> None:
    """Print one line for each table of ``replace`` whose element the branch holds: the run skips the element, so the rows stay."""
    for name in (n for n in REPLACEABLE if n in replace):
        element = REPLACE_ELEMENTS[name]
        if writer.has_attribute(element) if element == ICEBERG_TABLES else writer.has(element):
            print(f"--replace-tables {name}: {element} is on branch {writer.branch} already; held, not replaced", flush=True)


def held_element_conflicts(catalog, namespace: str, specs: dict, replace: set[str], handle, branch: str) -> list[str]:
    """One line for each table of ``replace`` with another schema whose element ``branch`` holds.

    The run would change the schema and skip the rows, because it skips a held element.
    """
    if handle is None or branch not in handle.list_branches():
        return []
    writer, found = ElementWriter(handle, branch), []
    for name in (n for n in ELEMENT_TABLES if n in replace and catalog.table_exists(f"{namespace}.{n}")):
        try:
            added, deleted = schema_changes(catalog.load_table(f"{namespace}.{name}"), specs[name]["schema"])
        except ValueError:  # The require step refuses a type change.
            continue
        if (added or deleted) and writer.has(REPLACE_ELEMENTS[name]):
            found.append(f"--replace-tables {name}: {REPLACE_ELEMENTS[name]} is on branch {branch} already, "
                         f"so the run changes the schema of {name} and skips its rows")
    return found


def _preflight_repo(repo: str, branch: str, prefixes: list[str], problems: list[str]):
    """Print whether the repo opens and whether ``branch`` exists or can start from ``main``; the repo, or ``None``."""
    try:
        handle = _open_existing_repo(repo, prefixes)
    except Exception as e:  # Any failure to open is a problem to report; the text has no location.
        problems.append(f"repo: does not open ({type(e).__name__})")
        return None
    if handle is None:
        print("repo: absent; the ingest creates it", flush=True)
        return None
    branches = handle.list_branches()
    if branch in branches:
        print(f"branch {branch}: exists", flush=True)
    elif "main" in branches:
        print(f"branch {branch}: created from main by the ingest", flush=True)
    else:
        problems.append(f"branch {branch}: absent, and the repo has no branch main to start it from")
    return handle


def preflight(*, mode: str, branch: str, repo: str, catalog: str, namespace: str, elements: str | None = None,
              replace_tables: str = "", root_keys: str | None = None, sample: str | None = None,
              record: Path | None = None) -> list[str]:
    """Check what needs no bundle and no write, print the plan, and return one line for each problem.

    With a ``sample`` its record, which is ``record`` or the committed one, must load.

    The check makes no commit and creates no table, namespace, branch or directory.
    """
    problems = []
    if mode != "full":
        problems.append("--preflight is an option of --mode full")
    if replace_tables and mode != "full":
        problems.append("--replace-tables is an option of --mode full")
    if branch in PROTECTED[mode]:
        problems.append(f"--mode {mode} never writes to the branch {branch}")
    if sample is not None:
        try:
            load_record(sample, path=record)
        except ValueError as e:
            problems.append(f"record: {e}")
    selected = replace = None
    try:
        selected = select_elements(elements, ELEMENTS[mode])
    except ValueError as e:
        problems.append(str(e))
    try:
        replace = replace_table_names(replace_tables) if replace_tables else set()
    except ValueError as e:
        problems.append(str(e))
    if selected is not None and replace:
        try:
            check_replace_elements(replace, selected)
        except ValueError as e:
            problems.append(str(e))
    if selected is not None and root_keys is not None:
        try:
            root_key_names(root_keys, selected)
        except ValueError as e:
            problems.append(str(e))
    try:
        require_same_place(repo, catalog)
    except SystemExit as e:
        return [*problems, str(e)]
    org = catalog_org(catalog)
    if org is None and not (Path(catalog) / "catalog.db").exists():
        print("catalog: absent; the ingest creates it", flush=True)
        _preflight_repo(repo, branch, [], problems)
        return problems
    try:
        from arraylake import Client

        opened = Client().get_iceberg(org) if org is not None else local_catalog(Path(catalog))
        if (namespace,) not in opened.list_namespaces():
            print(f"namespace {namespace}: absent; the ingest creates it", flush=True)
    except Exception as e:  # Any failure to open is a problem to report; the text has no location.
        problems.append(f"catalog: does not open ({type(e).__name__})")
        return problems
    handle = _preflight_repo(repo, branch, _preflight_tables(opened, namespace, replace or set(), problems), problems)
    try:
        problems += held_element_conflicts(opened, namespace, table_specs({}, []), replace or set(), handle, branch)
    except Exception as e:  # The text of a storage error can hold a location.
        problems.append(f"branch {branch}: elements do not read ({type(e).__name__})")
    return problems


def main() -> None:
    """Run the ingest. A refusal ends the run with its message on one line, with no traceback."""
    try:
        run()
    except HeldFilesError as e:
        raise SystemExit(f"{e}; use --replace-tables {e.table}") from None
    except ValueError as e:
        # A ValueError from another library is a fault, not a refusal: it keeps its traceback.
        if not _raised_here(e):
            raise
        raise SystemExit(str(e)) from None


def _raised_here(error: BaseException) -> bool:
    """True when the innermost frame of ``error`` is in this ingest or in ``minus80``."""
    import minus80

    frame = error.__traceback__
    while frame.tb_next is not None:
        frame = frame.tb_next
    source = Path(frame.tb_frame.f_code.co_filename).resolve()
    return any(source.is_relative_to(Path(root).resolve().parent) for root in (__file__, minus80.__file__))


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", choices=sorted(SAMPLES), required=True)
    parser.add_argument("--repo", required=True, help="a local directory, or arraylake://<org>/<repo>")
    parser.add_argument("--catalog", required=True,
                        help="a local directory for the catalog and its warehouse, or arraylake://<org>")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--mode", choices=sorted(PROTECTED), default="fixture",
                        help="fixture writes one level, one tile and the polygons; full writes every element")
    parser.add_argument("--branch", default="fixture")
    parser.add_argument("--concurrency", type=int, default=1, help="chunk writes that run at once inside an image or mask")
    parser.add_argument("--he-image", help="the H&E OME-TIFF, a URL or a local path; by default 10x's object")
    parser.add_argument("--he-alignment", help="the H&E alignment CSV, a URL or a local path; by default 10x's object")
    parser.add_argument("--record", type=Path,
                        help="--mode full: the record of the sample's bundle; by default the sample's committed record")
    parser.add_argument("--elements", help="write only these elements, comma-separated, of the mode's elements")
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    parser.add_argument("--replace-tables", default="",
                        help=f"--mode full: replace the rows of these held tables in place, comma-separated, of "
                             f"{', '.join(REPLACEABLE)}; the earlier snapshots and their files stay")
    parser.add_argument("--preflight", action="store_true",
                        help="--mode full: check the arguments, the repo, the catalog and each table with no bundle and no "
                             "write, print the plan, and exit 1 on a problem")
    parser.add_argument("--root-keys",
                        help=f"--mode full with --elements root alone: write only these root attributes, comma-separated, "
                             f"of {', '.join(ROOT_KEYS)}; another key that changes ends the run before the write. "
                             "--preflight checks the names only, as it has no bundle to build the values from")
    parser.add_argument("--rewrite", type=rewrite_elements, default=set(),
                        help=f"write only these held elements again, comma-separated, of {', '.join(REWRITABLE)}")
    args = parser.parse_args()
    if args.preflight:
        problems = preflight(mode=args.mode, branch=args.branch, repo=args.repo, catalog=args.catalog,
                             namespace=args.namespace, elements=args.elements, replace_tables=args.replace_tables,
                             root_keys=args.root_keys, sample=args.sample, record=args.record)
        for problem in problems:
            print(f"PROBLEM: {problem}")
        print(f"preflight: {len(problems)} problem{'s' * (len(problems) != 1)}" if problems else "preflight: no problem")
        raise SystemExit(1 if problems else 0)
    if args.rewrite and args.branch == "main":
        raise SystemExit("--rewrite never writes to main")
    if args.branch in PROTECTED[args.mode]:
        raise SystemExit(f"--mode {args.mode} never writes to the branch {args.branch}")
    if args.rewrite and args.elements:
        raise SystemExit("--elements does not apply to --rewrite; --rewrite names its own elements")
    if args.rewrite and args.mode == "full":
        raise SystemExit("--rewrite is an option of --mode fixture")
    if args.replace_tables and args.mode != "full":
        raise SystemExit("--replace-tables is an option of --mode full")
    if args.root_keys is not None and args.mode != "full":
        raise SystemExit("--root-keys is an option of --mode full")
    try:
        replace = replace_table_names(args.replace_tables) if args.replace_tables else set()
        selected = select_elements(args.elements, ELEMENTS[args.mode])
        check_replace_elements(replace, selected)
        root_keys = None if args.root_keys is None else root_key_names(args.root_keys, selected)
    except ValueError as e:
        raise SystemExit(str(e))
    require_same_place(args.repo, args.catalog)
    org = catalog_org(args.catalog)
    on_arraylake = org is not None

    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    sample, layouts = SAMPLES[args.sample], LAYOUTS.read_text()
    full = args.mode == "full"
    he_image = args.he_image or sample.url(HE_IMAGE, S3)
    he_alignment = args.he_alignment or sample.url(HE_ALIGNMENT, S3)
    record = load_record(args.sample, path=args.record) if full else None
    bundle, counter = open_bundle(args.bundle or sample.url(BUNDLE, S3))
    if not args.bundle:
        check_bundle_size(bundle, sample, layouts)
    if full:
        open_he, alignment_text = he_readers(he_image, he_alignment, counter.wrap, counter.wrap)
        prepared = prepare(bundle, sample, layouts, open_he, alignment_text, record=record)
        experiment, methods = prepared.experiment, prepared.methods
    else:
        prepared = None
        experiment, methods = bundle.experiment(), json.loads(bundle.member_zip(CELLS).read(".zattrs"))["segmentation_methods"]
    pixel_size = experiment["pixel_size"]

    if on_arraylake:
        from arraylake import Client

        tables = TableWriter(Client().get_iceberg(org), args.namespace, catalog_name="arraylake", org=org)
    else:
        tables = TableWriter(local_catalog(Path(args.catalog)), args.namespace, catalog_name="local", org="local")
    specs = table_specs(table_properties(sample, layouts, sample.repo), methods)
    # The tables exist before any element, so a repo without a container over their files fails here.
    if replace and (found := held_element_conflicts(tables.catalog, tables.namespace, specs, replace,
                                                    _open_existing_repo(args.repo, []), args.branch)):
        raise SystemExit("\n".join(found))
    require_tables(tables, specs, replace)
    prefixes = [tables.data_prefix(name) for name in ELEMENT_TABLES]
    if on_arraylake:
        repo = arraylake_repository(args.repo, source_prefixes=prefixes)
    else:
        repo = create_repo(args.repo, [], prefixes=prefixes, open_existing=True)
    if args.branch not in repo.list_branches():
        repo.create_branch(args.branch, repo.lookup_branch("main"))
    writer = ElementWriter(repo, args.branch, counter=counter, protected=PROTECTED[args.mode])

    report_held_replacements(writer, replace)
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
    steps = {
        "tables/table": unless_held(writer, "tables/table", lambda: write_table(writer, bundle, concurrency=n)),
        **{f"labels/{name}": partial(write_labels, writer, bundle, pixel_size, names=[name], concurrency=n)
           for name in MASKS},
        **{f"shapes/{name}": unless_held(writer, f"shapes/{name}",
                                         partial(write_boundaries, writer, bundle, tables, name, index,
                                                 replace=name in replace))
           for name, index in (("nucleus_boundaries", NUCLEUS_SET), ("cell_boundaries", CELL_SET))},
        "points/transcripts": unless_held(
            writer, "points/transcripts",
            lambda: write_transcripts(writer, bundle, tables, tiles=None if full else [FIXTURE_TILE],
                                      replace_held="transcripts" in replace)),
        ICEBERG_TABLES: lambda: write_extra_tables(writer, bundle, tables, sample, layouts, sample.repo, replace),
        "images/morphology": lambda: write_morphology(writer, bundle, pixel_size, levels=None if full else [FIXTURE_LEVEL],
                                                      concurrency=n),
        "images/morphology_3d": lambda: write_morphology_3d(writer, bundle, pixel_size, concurrency=n),
        "images/he_image": lambda: write_he(writer, he_image, he_alignment, pixel_size, concurrency=n),
        "root": lambda: write_root(writer, prepared.root if full else build_root(
            bundle, sample, layouts, experiment, "fixture", missing=MISSING_FROM_FIXTURE, fixture=FIXTURE), root_keys),
    }
    qc_selected = [e for e in selected if e.startswith(QC_PREFIX)]
    for element in selected:
        if element in qc_selected:
            # The first QC element writes all the selected rasters.
            if element == qc_selected[0]:
                write_qc(writer, prepared.qc, [e.removeprefix(QC_PREFIX) for e in qc_selected])
            continue
        steps[element]()
    print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
    print(f"{args.mode} written: {', '.join(selected)}")


if __name__ == "__main__":
    main()
