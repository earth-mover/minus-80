"""Write an Atera sample into a local Icechunk repo and a local Iceberg catalog.

``--fixture`` writes the fixture of PLAN.md, "Steps, scope and risk", step 1, onto
its own branch, one commit per element. The bundle is read in place by range requests.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
import zarr
from anndata.io import write_elem
from minus80.core.icechunk_io import commit, create_repo
from minus80.core.progress import Progress
from minus80.core.pyramid import channel_names, create_level, write_page
from minus80.core.tables import declare_copy, iceberg_table_attr, register_files, write_parquet
from minus80.core.zarr_copy import copy_chunks, copy_group, create_copy
from minus80.ome import channel_omero, image_attrs, label_attrs
from minus80.ome.spatialdata import points_attrs, raster_attrs, scale, shapes_attrs, table_attrs
from pyiceberg.catalog.sql import SqlCatalog

from bundle import CELLS, MATRIX, MORPHOLOGY_2D, TRANSCRIPTS, Bundle, read_v2_array
from polygons import GEO_POLYGONS, POLYGON_SCHEMA, polygon_table
from root_attrs import LICENSE, citation_text, parse_metrics, root_attrs, source_objects
from sources import S3, SAMPLES
from transcripts import TRANSCRIPT_SCHEMA, codeword_table, tile_table, xenium_cell_id

LAYOUTS = Path(__file__).with_name("LAYOUTS.md")

#: Label element, then the cells store's mask array; PLAN.md, judgement call 1.
MASKS = {"nucleus_labels": "masks/0", "cell_labels": "masks/1"}
NUCLEUS_SET = 0
FIXTURE_LEVEL = 6
FIXTURE_TILE = "0,0"
REGION_KEY, INSTANCE_KEY = "region", "instance"
#: The unit of the global coordinate system; PLAN.md, judgement call 4.
UNIT = "micrometer"
MIB = 1 << 20

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


class ReadCounter:
    """An obstore store that counts the bytes it fetches and advances the current progress line."""

    def __init__(self, store):
        self.store, self.bytes, self.progress = store, 0, None

    def get_ranges(self, path, *, starts, lengths):
        size = sum(lengths)
        self.bytes += size
        if self.progress is not None:
            self.progress.advance(size)
        return self.store.get_ranges(path, starts=starts, lengths=lengths)

    def __getattr__(self, name):
        return getattr(self.store, name)


def open_bundle(location: str) -> tuple[Bundle, ReadCounter]:
    bundle = Bundle(location)
    # BlockStoreReader fetches every block through its store, so this counts all network reads.
    counter = ReadCounter(bundle.file._store)
    bundle.file._store = counter
    return bundle, counter


def member_bytes(zf, prefix: str) -> int:
    return sum(i.compress_size for i in zf.infolist() if i.filename.startswith(prefix))


class Writer:
    """One writable session and one commit per element, with bytes read and wall time printed."""

    def __init__(self, repo, branch: str, counter: ReadCounter):
        self.repo, self.branch, self.counter = repo, branch, counter

    @contextmanager
    def element(self, path: str, total: int | None):
        session = self.repo.writable_session(self.branch)
        root = zarr.open_group(session.store, mode="a")
        start, before = time.monotonic(), self.counter.bytes
        with Progress(f"{path}: reading the bundle", total=total, unit="MiB", scale=MIB) as progress:
            self.counter.progress = progress
            try:
                yield root
            finally:
                self.counter.progress = None
        snapshot = commit(session, f"Write {path}")
        read, elapsed = self.counter.bytes - before, time.monotonic() - start
        print(f"{path}: {read / MIB:,.1f} MiB read, {elapsed:,.1f} s, commit {snapshot}", flush=True)


def write_table(writer: Writer, bundle: Bundle) -> None:
    zf = bundle.member_zip(MATRIX)
    with writer.element("tables/table", member_bytes(zf, "")) as root:
        group = root.require_group("tables").create_group("table")
        copy_group(zf, "", group)
        obs = group["obs"]
        n = group["X"].attrs["shape"][0]
        # SpatialData needs a region column and the mask label as the instance key; the source has neither.
        write_elem(obs, REGION_KEY, pd.Categorical(["cell_labels"] * n))
        write_elem(obs, INSTANCE_KEY, np.arange(1, n + 1, dtype=np.int64))
        obs.attrs["column-order"] = [*obs.attrs["column-order"], REGION_KEY, INSTANCE_KEY]
        keys = {"region": "cell_labels", "region_key": REGION_KEY, "instance_key": INSTANCE_KEY}
        # SpatialData reads the keys from uns; the group attributes only mirror them.
        write_elem(group, "uns", {"spatialdata_attrs": keys})
        group.attrs.update(table_attrs(keys["region"], region_key=REGION_KEY, instance_key=INSTANCE_KEY))


def _single_level(ome: dict, path: str) -> dict:
    """``ome`` with its only level stored at ``path``."""
    ome["multiscales"][0]["datasets"][0]["path"] = path
    return ome


def write_labels(writer: Writer, bundle: Bundle, pixel_size: float) -> None:
    zf = bundle.member_zip(CELLS)
    for name, mask in MASKS.items():
        with writer.element(f"labels/{name}", member_bytes(zf, f"{mask}/")) as root:
            group = root.require_group("labels").create_group(name)
            array = create_copy(group, "s0", json.loads(zf.read(f"{mask}/.zarray")), dimension_names=("y", "x"))
            copy_chunks(zf, mask, array)
            ome = label_attrs(name=name, dims=("y", "x"), level_shapes=[array.shape],
                              scale={"y": 1.0, "x": 1.0}, source_image="../../images/morphology")
            group.attrs.update(raster_attrs(_single_level(ome, "s0"), scale([pixel_size] * 2, ("y", "x"), unit=UNIT)))


def write_morphology(writer: Writer, bundle: Bundle, pixel_size: float) -> None:
    tiffs = [bundle.tiff(name) for name in MORPHOLOGY_2D]
    with writer.element(f"images/morphology/s{FIXTURE_LEVEL}", None) as root:
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


def local_catalog(directory: Path) -> SqlCatalog:
    """The SQLite catalog in ``directory``, whose warehouse is ``directory`` too."""
    directory = directory.resolve()
    return SqlCatalog("local", uri=f"sqlite:///{directory}/catalog.db", warehouse=directory.as_uri())


class Tables:
    """A local Iceberg catalog whose warehouse holds the Parquet files the repo references."""

    def __init__(self, directory: Path, namespace: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.warehouse, self.namespace = directory.resolve(), namespace
        self.catalog = local_catalog(directory)
        if (namespace,) not in self.catalog.list_namespaces():
            self.catalog.create_namespace(namespace)

    def write(self, name: str, table, *, schema, properties, geo=None, partition_by=None):
        data = self.warehouse / self.namespace / name / "data"
        data.mkdir(parents=True, exist_ok=True)
        path = data / f"{name}-0.parquet"
        write_parquet(table, str(path), index_column="instance", geo=geo)
        ref = register_files(self.catalog, f"{self.namespace}.{name}", [path.as_uri()], schema=schema,
                             properties=properties, partition_by=partition_by)
        attr = iceberg_table_attr(ref, org="local", namespace=self.namespace, table=name, index_column="instance",
                                  geometry_column="geometry" if geo else None)
        # The local catalog stands in for Arraylake's in a fixture, so the attribute names it.
        return ref, {**attr, "catalog": "local"}


def write_nucleus_boundaries(writer, bundle, tables: Tables, properties: dict) -> None:
    zf = bundle.member_zip(CELLS)
    path = f"polygon_sets/{NUCLEUS_SET}/"
    with writer.element("shapes/nucleus_boundaries", member_bytes(zf, path) + member_bytes(zf, "cell_id/")) as root:
        cell_id = read_v2_array(zf, "cell_id")
        methods = json.loads(zf.read(".zattrs"))["segmentation_methods"]
        table = polygon_table(zf, NUCLEUS_SET, xenium_cell_id(cell_id[:, 0], cell_id[:, 1]))
        ref, attr = tables.write("nucleus_boundaries", table, schema=POLYGON_SCHEMA, geo=GEO_POLYGONS,
                                 properties={**properties, "geo": json.dumps(GEO_POLYGONS),
                                             "method_names": json.dumps(methods)})
        group = root.require_group("shapes").create_group("nucleus_boundaries")
        group.attrs.update({**shapes_attrs("POLYGON", scale([1.0, 1.0], ("x", "y"), unit=UNIT)), "iceberg_table": attr})
        declare_copy(group, "shapes.parquet", ref.files)


def write_transcripts(writer, bundle, tables: Tables, properties: dict) -> None:
    zf = bundle.member_zip(TRANSCRIPTS)
    total = member_bytes(zf, f"grid/{FIXTURE_TILE}/") + member_bytes(zf, "codeword_category/")
    with writer.element("points/transcripts", total) as root:
        table = tile_table(zf, FIXTURE_TILE, codeword_table(zf), first_instance=0)
        ref, attr = tables.write(
            "transcripts", table, schema=TRANSCRIPT_SCHEMA, partition_by="grid_tile",
            properties={**properties, "quality_score": "Widened from the source's float16 to float32."},
        )
        group = root.require_group("points").create_group("transcripts")
        transform = scale([1.0, 1.0, 1.0], ("x", "y", "z"), unit=UNIT)
        group.attrs.update({**points_attrs(("x", "y", "z"), transform, instance_key="cell_id",
                                           feature_key="feature_name"), "iceberg_table": attr})
        declare_copy(group, "points.parquet", ref.files)


def write_root(writer, bundle, sample, layouts: str, experiment: dict) -> None:
    metrics_name = next(n for n in bundle.names if n.startswith("metrics_summary_"))
    with writer.element("root attributes", None) as root:
        with bundle.member_file(metrics_name) as f:
            metrics = parse_metrics(f.read().decode())
        root.attrs.update(root_attrs(sample, layouts, experiment, metrics,
                                     missing=MISSING_FROM_FIXTURE, fixture=FIXTURE))


def check_bundle_size(bundle: Bundle, sample, layouts: str) -> None:
    recorded = source_objects(layouts, sample)[0]
    size = bundle.file.seek(0, os.SEEK_END)
    if size != recorded["size"]:
        raise ValueError(f"{recorded['url']} is {size} bytes; LAYOUTS.md records {recorded['size']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", required=True)
    parser.add_argument("--sample", choices=sorted(SAMPLES), required=True)
    parser.add_argument("--repo", required=True, help="a new local directory")
    parser.add_argument("--catalog", required=True, help="a local directory for the catalog and its warehouse")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--branch", default="fixture")
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    args = parser.parse_args()
    if "arraylake" in (args.repo + args.catalog):
        raise SystemExit("Arraylake targets are not implemented; pass local directories.")

    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    sample, layouts = SAMPLES[args.sample], LAYOUTS.read_text()
    bundle, counter = open_bundle(args.bundle or sample.url("_outs.zip", S3))
    check_bundle_size(bundle, sample, layouts)
    experiment = bundle.experiment()
    pixel_size = experiment["pixel_size"]

    tables = Tables(Path(args.catalog), args.namespace)
    repo = create_repo(args.repo, [], prefixes=[f"{tables.warehouse.as_uri()}/"])
    repo.create_branch(args.branch, repo.lookup_branch("main"))
    writer = Writer(repo, args.branch, counter)
    properties = table_properties(sample, layouts, sample.repo)

    started = time.monotonic()
    write_table(writer, bundle)
    write_labels(writer, bundle, pixel_size)
    write_morphology(writer, bundle, pixel_size)
    write_nucleus_boundaries(writer, bundle, tables, properties)
    write_transcripts(writer, bundle, tables, properties)
    write_root(writer, bundle, sample, layouts, experiment)
    print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
    print("fixture written: 1 image level, 2 labels, 1 shapes, 1 points, 1 table")


if __name__ == "__main__":
    main()
