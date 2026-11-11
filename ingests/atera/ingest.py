"""Write an Atera sample into an Icechunk repo and an Iceberg catalog, local or on Arraylake.

``--fixture`` writes the fixture of PLAN.md, "Steps, scope and risk", step 1, onto
its own branch, one commit per element. The bundle is read in place by range requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd
import zarr
from anndata.io import write_elem
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, create_repo
from minus80.core.progress import Progress
from minus80.core.registry import file_path
from minus80.core.pyramid import channel_names, create_level, write_page
from minus80.core.tables import declare_copy, iceberg_table_attr, register_files, require_table, write_parquet
from minus80.core.zarr_copy import copy_chunks, copy_group, create_copy
from minus80.ome import channel_omero, image_attrs, label_attrs
from minus80.ome.spatialdata import points_attrs, raster_attrs, scale, shapes_attrs, write_table_keys
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.schema import index_by_name

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

    def holds(self, path: str) -> bool:
        """True when the branch has a node at ``path``, so a rerun skips that element.

        An element is held only when its commit exists. An element that raised was never committed.
        """
        try:
            root = zarr.open_group(self.repo.readonly_session(branch=self.branch).store, mode="r")
        except zarr.errors.GroupNotFoundError:  # A branch with no commit beyond the first has no root group.
            return False
        return path in root

    def skips(self, path: str) -> bool:
        """True, with a printed line, when the branch holds ``path`` from an earlier run."""
        if self.holds(path):
            print(f"{path}: on branch {self.branch} already, skipped", flush=True)
            return True
        return False

    @contextmanager
    def element(self, path: str, total: int | None):
        """A writable root for ``path``; it commits only when the body returns without an exception."""
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


def drop_index_column(frame: zarr.Group) -> None:
    """Remove the index from the data frame's ``column-order``; the index array stays.

    The source lists the index in ``column-order`` too, so a reader sees the field twice.
    """
    frame.attrs["column-order"] = [c for c in frame.attrs["column-order"] if c != frame.attrs["_index"]]


def write_table(writer: Writer, bundle: Bundle) -> None:
    zf = bundle.member_zip(MATRIX)
    with writer.element("tables/table", member_bytes(zf, "")) as root:
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


def write_table_index(writer: Writer) -> None:
    """Write the ``obs`` and ``var`` attributes of a held ``tables/table`` again, index only."""
    with writer.element("tables/table obs and var attributes", None) as root:
        drop_index_column(root["tables/table/obs"])
        drop_index_column(root["tables/table/var"])


def _single_level(ome: dict, path: str) -> dict:
    """``ome`` with its only level stored at ``path``."""
    ome["multiscales"][0]["datasets"][0]["path"] = path
    return ome


def write_labels(writer: Writer, bundle: Bundle, pixel_size: float) -> None:
    zf = bundle.member_zip(CELLS)
    for name, mask in MASKS.items():
        if writer.skips(f"labels/{name}"):
            continue
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
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    return SqlCatalog("local", uri=f"sqlite:///{directory}/catalog.db", warehouse=directory.as_uri())


def write_local(io, source: Path, url: str) -> None:
    """Copy ``source`` to the ``file://`` URL ``url``; an existing file raises ``FileExistsError``."""
    path = file_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as data, path.open("xb") as target:
        shutil.copyfileobj(data, target, 16 * MIB)


def write_through_io(io, source: Path, url: str) -> None:
    """Copy ``source`` to ``url`` through the table's FileIO; an existing object raises ``FileExistsError``."""
    with source.open("rb") as data, io.new_output(url).create(overwrite=False) as target:
        shutil.copyfileobj(data, target, 16 * MIB)


def content_name(path: Path) -> str:
    """The first 32 hex digits of the SHA-256 of the file at ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(16 * MIB):
            digest.update(block)
    return digest.hexdigest()[:32]


class Tables:
    """One Iceberg namespace. Each table's Parquet file goes under the table's own location."""

    def __init__(self, catalog, namespace: str, *, kind: str, org: str, write_file):
        self.catalog, self.namespace = catalog, namespace
        self.kind, self.org, self.write_file = kind, org, write_file
        if (namespace,) not in self.catalog.list_namespaces():
            self.catalog.create_namespace(namespace)

    def require(self, name: str, *, schema, properties, partition_by=None):
        """The table ``name``, created when absent, with ``properties`` set."""
        table = require_table(self.catalog, f"{self.namespace}.{name}", schema=schema,
                              properties=properties, partition_by=partition_by)
        if any(table.properties.get(k) != v for k, v in properties.items()):
            with table.transaction() as transaction:
                transaction.set_properties(dict(properties))
            table = self.catalog.load_table(f"{self.namespace}.{name}")
        return table

    def data_prefix(self, name: str, **spec) -> str:
        """The URL prefix that holds the table's Parquet files; it creates the table when absent."""
        return f"{self.require(name, **spec).location()}/data/"

    def write(self, name: str, table, *, schema, properties, geo=None, partition_by=None, replace=False):
        """Write ``table`` as a file of the table, with the table's field ids, and register it.

        The file name holds a hash of the file's bytes, so other bytes never take an existing name.
        An object under that name is reused and never written again, so a committed reference stays valid.
        With ``replace``, the file replaces every file the table holds.
        With ``replace``, a failure after the table snapshot and before the repo commit leaves one
        extra snapshot, and the next run's snapshot leaves that file unreferenced. The next run converges.
        """
        spec = {"schema": schema, "properties": properties, "partition_by": partition_by}
        iceberg = self.require(name, **spec)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.parquet"
            write_parquet(table, str(path), index_column="instance", geo=geo,
                          field_ids=index_by_name(iceberg.schema()))
            url = f"{iceberg.location()}/data/{name}-{content_name(path)}.parquet"
            if not iceberg.io.new_input(url).exists():
                self.write_file(iceberg.io, path, url)
        ref = register_files(self.catalog, f"{self.namespace}.{name}", [url], replace=replace, **spec)
        attr = iceberg_table_attr(ref, catalog=self.kind, org=self.org, namespace=self.namespace, table=name,
                                  index_column="instance", geometry_column="geometry" if geo else None)
        return ref, attr


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


def write_nucleus_boundaries(writer, bundle, tables: Tables, spec: dict, *, replace=False) -> None:
    zf = bundle.member_zip(CELLS)
    path = f"polygon_sets/{NUCLEUS_SET}/"
    with writer.element("shapes/nucleus_boundaries", member_bytes(zf, path) + member_bytes(zf, "cell_id/")) as root:
        cell_id = read_v2_array(zf, "cell_id")
        table = polygon_table(zf, NUCLEUS_SET, xenium_cell_id(cell_id[:, 0], cell_id[:, 1]))
        ref, attr = tables.write("nucleus_boundaries", table, geo=GEO_POLYGONS, replace=replace, **spec)
        group = root.require_group("shapes").create_group("nucleus_boundaries", overwrite=replace)
        group.attrs.update({**shapes_attrs("POLYGON", scale([1.0, 1.0], ("x", "y"), unit=UNIT)), "iceberg_table": attr})
        declare_copy(group, "shapes.parquet", ref.files)


def write_transcripts(writer, bundle, tables: Tables, spec: dict, *, replace=False) -> None:
    zf = bundle.member_zip(TRANSCRIPTS)
    total = member_bytes(zf, f"grid/{FIXTURE_TILE}/") + member_bytes(zf, "codeword_category/")
    with writer.element("points/transcripts", total) as root:
        table = tile_table(zf, FIXTURE_TILE, codeword_table(zf), first_instance=0)
        ref, attr = tables.write("transcripts", table, replace=replace, **spec)
        group = root.require_group("points").create_group("transcripts", overwrite=replace)
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


def rewrite_elements(text: str) -> set[str]:
    """The comma-separated elements of ``--rewrite``; each must be in :data:`REWRITABLE`."""
    elements = set(text.split(","))
    if unknown := elements - set(REWRITABLE):
        raise ValueError(f"cannot rewrite {', '.join(sorted(unknown))}; rewritable: {', '.join(REWRITABLE)}")
    return elements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", required=True)
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
        tables = Tables(Client().get_iceberg(org), args.namespace, kind="arraylake", org=org,
                        write_file=write_through_io)
    else:
        tables = Tables(local_catalog(Path(args.catalog)), args.namespace, kind="local", org="local",
                        write_file=write_local)
    methods = json.loads(bundle.member_zip(CELLS).read(".zattrs"))["segmentation_methods"]
    specs = table_specs(table_properties(sample, layouts, sample.repo), methods)
    # The tables exist before any element, so a repo without a container over their files fails here.
    prefixes = [tables.data_prefix(name, **spec) for name, spec in specs.items()]
    if on_arraylake:
        repo = arraylake_repository(args.repo, source_prefixes=prefixes)
    else:
        repo = create_repo(args.repo, [], prefixes=prefixes, open_existing=True)
    if args.branch not in repo.list_branches():
        repo.create_branch(args.branch, repo.lookup_branch("main"))
    writer = Writer(repo, args.branch, counter)

    started = time.monotonic()
    if args.rewrite:
        # A rewrite touches only the named elements; the tables keep their UUIDs.
        if "tables/table:attrs" in args.rewrite:
            write_table_index(writer)
        if "shapes/nucleus_boundaries" in args.rewrite:
            write_nucleus_boundaries(writer, bundle, tables, specs["nucleus_boundaries"], replace=True)
        if "points/transcripts" in args.rewrite:
            write_transcripts(writer, bundle, tables, specs["transcripts"], replace=True)
        print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
        print(f"rewritten: {', '.join(sorted(args.rewrite))}")
        return
    if not writer.skips("tables/table"):
        write_table(writer, bundle)
    write_labels(writer, bundle, pixel_size)
    if not writer.skips(f"images/morphology/s{FIXTURE_LEVEL}"):
        write_morphology(writer, bundle, pixel_size)
    if not writer.skips("shapes/nucleus_boundaries"):
        write_nucleus_boundaries(writer, bundle, tables, specs["nucleus_boundaries"])
    if not writer.skips("points/transcripts"):
        write_transcripts(writer, bundle, tables, specs["transcripts"])
    write_root(writer, bundle, sample, layouts, experiment)
    print(f"bundle: {counter.bytes / MIB:,.1f} MiB read in {time.monotonic() - started:,.1f} s")
    print("fixture written: 1 image level, 2 labels, 1 shapes, 1 points, 1 table")


if __name__ == "__main__":
    main()
