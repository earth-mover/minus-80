"""Write the fixture of an Atera sample into an Icechunk repo and an Iceberg catalog, local or on Arraylake.

The script has one mode: the fixture of PLAN.md, "Steps, scope and risk", step 1. It writes onto
its own branch, one commit per element. The full ingest is stage-two work, in PLAN.md, "Stage two".
The bundle is read in place by range requests.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd
import zarr
from anndata.io import write_elem
from minus80.core.elements import ElementWriter
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo
from minus80.core.progress import MIB
from minus80.core.registry import as_url
from minus80.core.remote import ReadCounter, counting_reader
from minus80.core.pyramid import channel_names, create_level, write_page
from minus80.core.tables import TableWriter, declare_copy, iceberg_table_attr, local_catalog
from minus80.core.zarr_copy import copy_chunks, copy_group, create_copy, read_array
from minus80.ome import channel_omero, image_attrs, label_attrs
from minus80.ome.spatialdata import points_attrs, raster_attrs, scale, shapes_attrs, write_table_keys

from bundle import CELLS, MATRIX, MORPHOLOGY_2D, TRANSCRIPTS, Bundle
from polygons import GEO_POLYGONS, POLYGON_SCHEMA, polygon_table
from root_attrs import LICENSE, citation_text, parse_metrics, root_attrs, source_objects
from sources import S3, SAMPLES
from transcripts import TRANSCRIPT_SCHEMA, codeword_table, tile_order, write_tile, xenium_cell_id

LAYOUTS = Path(__file__).with_name("LAYOUTS.md")

#: Label element, then the cells store's mask array; PLAN.md, judgement call 1.
MASKS = {"nucleus_labels": "masks/0", "cell_labels": "masks/1"}
NUCLEUS_SET = 0
FIXTURE_LEVEL = 6
FIXTURE_TILE = "0,0"
REGION_KEY, INSTANCE_KEY = "region", "instance"
#: The unit of the global coordinate system; PLAN.md, judgement call 4.
UNIT = "micrometer"
#: The elements ``--rewrite`` can write again; ``tables/table:attrs`` is the ``obs`` and ``var`` attributes.
REWRITABLE = ("points/transcripts", "shapes/nucleus_boundaries", "tables/table:attrs")

FIXTURE = (
    "This branch is a fixture, not the full store. It holds one level of images/morphology (s6), "
    f"one transcript tile (grid {FIXTURE_TILE}) and one polygon set (nucleus)."
)

MISSING_FROM_FIXTURE = [
    {"what": "images/morphology levels 0 to 5 and 7", "why": "The fixture writes level 6 only."},
    {"what": "images/morphology_3d, images/he_image, images/overview_scan, images/qc_*", "why": "The fixture leaves them out."},
    {"what": f"points/transcripts tiles other than {FIXTURE_TILE}", "why": "The fixture writes one tile."},
    {"what": "shapes/cell_boundaries", "why": "The fixture writes the nucleus polygon set only."},
    {"what": "Iceberg tables codewords, diffexp_azimuth, diffexp_graphclust", "why": "The fixture leaves them out."},
    {"what": "csc_cell_feature_matrix.zarr.zip", "why": "It is a copy of X ordered by column."},
    {"what": "binned_transcripts.zarr.zip", "why": "It is derived from the transcripts for 10x Explorer."},
    {"what": "the HTML reports' thumbnails, plots and interface", "why": "They are not data."},
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
        write_elem(obs, REGION_KEY, pd.Categorical(["cell_labels"] * n))
        write_elem(obs, INSTANCE_KEY, np.arange(1, n + 1, dtype=np.int64))
        obs.attrs["column-order"] = [*obs.attrs["column-order"], REGION_KEY, INSTANCE_KEY]
        write_table_keys(group, "cell_labels", region_key=REGION_KEY, instance_key=INSTANCE_KEY)


def write_table_index(writer: ElementWriter) -> None:
    """Write the ``obs`` and ``var`` attributes of a held ``tables/table`` again, index only."""
    with writer.unit("tables/table obs and var attributes", "Write tables/table obs and var attributes") as (root, _):
        drop_index_column(root["tables/table/obs"])
        drop_index_column(root["tables/table/var"])


def _single_level(ome: dict, path: str) -> dict:
    """``ome`` with its only level stored at ``path``."""
    ome["multiscales"][0]["datasets"][0]["path"] = path
    return ome


def write_labels(writer: ElementWriter, bundle: Bundle, pixel_size: float) -> None:
    zf = bundle.member_zip(CELLS)
    for name, mask in MASKS.items():
        if skip_held(writer, f"labels/{name}"):
            continue
        with writer.unit(f"labels/{name}", f"Write labels/{name}", total=member_bytes(zf, f"{mask}/")) as (root, _):
            group = root.require_group("labels").create_group(name)
            array = create_copy(group, "s0", json.loads(zf.read(f"{mask}/.zarray")), dimension_names=("y", "x"))
            copy_chunks(zf, mask, array)
            ome = label_attrs(name=name, dims=("y", "x"), level_shapes=[array.shape],
                              scale={"y": 1.0, "x": 1.0}, source_image="../../images/morphology")
            group.attrs.update(raster_attrs(_single_level(ome, "s0"), scale([pixel_size] * 2, ("y", "x"), unit=UNIT)))


def write_morphology(writer: ElementWriter, bundle: Bundle, pixel_size: float) -> None:
    tiffs = [bundle.tiff(name) for name in MORPHOLOGY_2D]
    with writer.unit(f"images/morphology/s{FIXTURE_LEVEL}", f"Write images/morphology/s{FIXTURE_LEVEL}") as (root, _):
        names = channel_names(tiffs[0].pages.first.description)
        full = tiffs[0].series[0].levels[0].shape
        pages = [tif.series[0].levels[FIXTURE_LEVEL].pages[0] for tif in tiffs]
        shape = (len(pages), *pages[0].shape)
        group = root.require_group("images").create_group("morphology")
        array = create_level(group, f"s{FIXTURE_LEVEL}", shape, pages[0].dtype, dims=("c", "y", "x"))
        for channel, page in enumerate(pages):
            write_page(array, page, (channel,))
        ome = image_attrs(
            name="morphology", dims=("c", "y", "x"), level_shapes=[shape], scale={"c": 1.0, "y": 1.0, "x": 1.0},
            omero=channel_omero([{"label": n} for n in names], max_value=np.iinfo(array.dtype).max),
        )
        # The level's pixel is full / level times the full-resolution pixel.
        factors = [1.0, pixel_size * full[0] / shape[1], pixel_size * full[1] / shape[2]]
        group.attrs.update(raster_attrs(_single_level(ome, f"s{FIXTURE_LEVEL}"), scale(factors, ("c", "y", "x"), unit=UNIT)))


def table_properties(sample, layouts: str, repo: str, **extra) -> dict[str, str]:
    """Attribution on every table, so a table read on its own still attributes."""
    properties = {
        "license": json.dumps(LICENSE),
        "citation": citation_text(sample),
        "source": json.dumps(source_objects(layouts, sample)),
        "referenced_by": f"The uint8 arrays of the Icechunk repo {repo} reference this table's data files.",
    }
    return properties | {k: v if isinstance(v, str) else json.dumps(v) for k, v in extra.items()}


def table_attr(tables: TableWriter, name: str, ref, *, geo=None) -> dict:
    """The ``iceberg_table`` attribute of the element that references table ``name`` at ``ref``."""
    return iceberg_table_attr(ref, catalog=tables.catalog_name, org=tables.org, namespace=tables.namespace,
                              table=name, index_column=tables.index_column, geometry_column="geometry" if geo else None)


def table_specs(properties: dict, methods: list[str]) -> dict[str, dict]:
    """Each Iceberg table's schema, partitioning and properties, known before any element is written."""
    return {
        "nucleus_boundaries": {
            "schema": POLYGON_SCHEMA,
            "properties": {**properties, "geo": json.dumps(GEO_POLYGONS), "method_names": json.dumps(methods)},
        },
        "transcripts": {
            "schema": TRANSCRIPT_SCHEMA,
            "partition_by": "grid_tile",
            "properties": {**properties, "quality_score": "Widened from the source's float16 to float32."},
        },
    }


def write_nucleus_boundaries(writer, bundle, tables: TableWriter, *, replace=False) -> None:
    zf = bundle.member_zip(CELLS)
    path = f"polygon_sets/{NUCLEUS_SET}/"
    total = member_bytes(zf, path) + member_bytes(zf, "cell_id/")
    with writer.unit("shapes/nucleus_boundaries", "Write shapes/nucleus_boundaries", total=total) as (root, _):
        cell_id = read_array(zf, "cell_id")
        table = polygon_table(zf, NUCLEUS_SET, xenium_cell_id(cell_id[:, 0], cell_id[:, 1]))
        ref = tables.write("nucleus_boundaries", table, geo=GEO_POLYGONS, replace=replace)
        attr = table_attr(tables, "nucleus_boundaries", ref, geo=GEO_POLYGONS)
        group = root.require_group("shapes").create_group("nucleus_boundaries", overwrite=replace)
        group.attrs.update({**shapes_attrs("POLYGON", scale([1.0, 1.0], ("x", "y"), unit=UNIT)), "iceberg_table": attr})
        declare_copy(group, "shapes.parquet", ref.files)


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
        codewords, field_ids = codeword_table(zf), tables.field_ids("transcripts")
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


def write_root(writer, bundle, sample, layouts: str, experiment: dict) -> None:
    metrics_name = next(n for n in bundle.names if n.startswith("metrics_summary_"))
    with writer.unit("root attributes", "Write root attributes") as (root, _):
        with bundle.member_file(metrics_name) as f:
            metrics = parse_metrics(f.read().decode())
        root.attrs.update(root_attrs(sample, layouts, experiment, metrics,
                                     missing=MISSING_FROM_FIXTURE, fixture=FIXTURE))


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", choices=sorted(SAMPLES), required=True)
    parser.add_argument("--repo", required=True, help="a local directory, or arraylake://<org>/<repo>")
    parser.add_argument("--catalog", required=True,
                        help="a local directory for the catalog and its warehouse, or arraylake:<org>")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--branch", default="fixture")
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    parser.add_argument("--rewrite", type=rewrite_elements, default=set(),
                        help=f"write only these held elements again, comma-separated, of {', '.join(REWRITABLE)}")
    args = parser.parse_args()
    if args.rewrite and args.branch == "main":
        raise SystemExit("--rewrite never writes to main")
    on_arraylake = args.catalog.startswith("arraylake:")
    if on_arraylake != args.repo.startswith(ARRAYLAKE):
        raise SystemExit("--repo and --catalog must both be local or both be on Arraylake.")

    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    sample, layouts = SAMPLES[args.sample], LAYOUTS.read_text()
    bundle, counter = open_bundle(args.bundle or sample.url("_outs.zip", S3))
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
    for name, spec in specs.items():
        tables.require(name, **spec)
    prefixes = [tables.data_prefix(name) for name in specs]
    if on_arraylake:
        repo = arraylake_repository(args.repo, source_prefixes=prefixes)
    else:
        repo = create_repo(args.repo, [], prefixes=prefixes, open_existing=True)
    if args.branch not in repo.list_branches():
        repo.create_branch(args.branch, repo.lookup_branch("main"))
    writer = ElementWriter(repo, args.branch, counter=counter, protected=("main",) if args.rewrite else ())

    started = time.monotonic()
    if args.rewrite:
        # A rewrite touches only the named elements; the tables keep their UUIDs.
        if "tables/table:attrs" in args.rewrite:
            write_table_index(writer)
        if "shapes/nucleus_boundaries" in args.rewrite:
            write_nucleus_boundaries(writer, bundle, tables, replace=True)
        if "points/transcripts" in args.rewrite:
            write_transcripts(writer, bundle, tables, tiles=[FIXTURE_TILE], replace=True)
        print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
        print(f"rewritten: {', '.join(sorted(args.rewrite))}")
        return
    if not skip_held(writer, "tables/table"):
        write_table(writer, bundle)
    write_labels(writer, bundle, pixel_size)
    if not skip_held(writer, f"images/morphology/s{FIXTURE_LEVEL}"):
        write_morphology(writer, bundle, pixel_size)
    if not skip_held(writer, "shapes/nucleus_boundaries"):
        write_nucleus_boundaries(writer, bundle, tables)
    if not skip_held(writer, "points/transcripts"):
        write_transcripts(writer, bundle, tables, tiles=[FIXTURE_TILE])
    write_root(writer, bundle, sample, layouts, experiment)
    print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
    print("fixture written: 1 image level, 2 labels, 1 shapes, 1 points, 1 table")


if __name__ == "__main__":
    main()
