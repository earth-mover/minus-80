import hashlib
import http.server
import io
import json
import zipfile
import shutil
import sys
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import scipy.sparse
import zarr
from anndata.io import write_elem
from pyiceberg.io.pyarrow import parquet_file_to_data_file
from pyiceberg.types import LongType
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

import ingest
import verify
from minus80.core.elements import COMPLETE, ElementWriter
from minus80.core.registry import file_path
from minus80.core.zarr_copy import copy_group
from minus80.core.icechunk_io import commit, create_repo
from minus80.ome.spatialdata import write_table_keys
from minus80.core.tables import (
    COPY_CHUNK, TableWriter, declare_copy, iceberg_table_attr, local_catalog, register_files, write_parquet,
)
from tests.stores import cells_zip, zip_directory, zip_v2_store
from facts import bundle_facts, write_record
from tests.test_images import ALIGNMENT, PIXEL, _rgb_pyramid, image_members, write_bundle
from tests.test_ingest_tables import TILES, _Bundle, _hold, _matrix_zip, _transcripts_run, _transcripts_zip
from tests.test_qc import DENSITY, _bag, _html, _resource
from bundle import CELLS, MATRIX, Bundle, he_readers, open_bundle
from ingest import (
    ICEBERG_TABLES, MISSING_FROM_FULL, catalog_org, table_attr, table_specs, write_extra_tables,
    write_labels,
)
from polygons import polygon_table
from root_attrs import KIND_SENTENCES, describe, element_kind
from sources import SAMPLES
from verify import (
    apply_transform, chunk_key, chunk_members, element_tables, gate_attrs, gate_complete, gate_copies,
    gate_extra_tables, gate_field_ids, gate_images, gate_labels, gate_matrix, gate_polygons, gate_qc, gate_source,
    gate_table_index, gate_table_region, gate_tables, gate_transforms, http_head, run_gates, select_gates,
    without_source,
)

LAYOUTS = (Path(__file__).parents[1] / "LAYOUTS.md").read_text()
SCHEMA = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string())])


def _write(tables, name, table, replace=False):
    """``table`` written into table ``name``, required with ``SCHEMA``; the reference and the element attribute."""
    tables.require(name, schema=SCHEMA, properties={})
    ref = tables.write(name, table, replace=replace)
    return ref, table_attr(tables, name, ref)


def _table_root(tmp_path, *, index_in_order: bool):
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    table = root.require_group("tables").require_group("table")
    write_elem(table, "obs", pd.DataFrame({"n": [1, 2]}, index=pd.Index(["a", "b"], name="barcode")))
    write_elem(table, "var", pd.DataFrame({"m": [3]}, index=pd.Index(["g"], name="feature_id")))
    if index_in_order:
        table["obs"].attrs["column-order"] = ["barcode", "n"]
    return root


def test_table_index_passes_an_index_only_frame(tmp_path):
    assert gate_table_index(_table_root(tmp_path, index_in_order=False)) == []


def test_table_index_fails_when_the_index_is_also_a_column(tmp_path):
    errors = gate_table_index(_table_root(tmp_path, index_in_order=True))
    assert errors == ["obs: index 'barcode' is also in column-order"]


def _points_root(tmp_path, attr=None):
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    root.require_group("shapes")
    points = root.require_group("points")
    if attr is not None:
        points.require_group("t").attrs["iceberg_table"] = attr
    return root


def test_field_ids_fails_when_no_element_has_a_table(tmp_path):
    assert gate_field_ids(_points_root(tmp_path), local_catalog(tmp_path / "c")) == [
        "no element has an iceberg_table attribute"
    ]


def test_field_ids_fails_a_snapshot_without_data_files(tmp_path):
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    _, attr = _write(tables, "t", pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=SCHEMA))
    iceberg = tables.catalog.load_table("ns.t")
    iceberg.delete()
    empty = tables.catalog.load_table("ns.t").current_snapshot().snapshot_id
    errors = gate_field_ids(_points_root(tmp_path, {**attr, "snapshot_id": empty}), tables.catalog)
    assert errors == [f"points/t: snapshot {empty} has no data files"]


def test_field_ids_checks_the_pinned_snapshot_and_every_field(tmp_path):
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    table = pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=SCHEMA)
    _, attr = _write(tables, "t", table)
    # A later schema adds a field the pinned snapshot's file never had.
    with tables.catalog.load_table("ns.t").update_schema() as update:
        update.add_column("extra", LongType())
    assert gate_field_ids(_points_root(tmp_path, attr), tables.catalog) == []
    newer = tables.catalog.load_table("ns.t")
    path = str(tmp_path / "short.parquet")
    short = pa.schema([pa.field("instance", pa.int64(), nullable=False)])
    pq.write_table(pa.table({"instance": [2]}, schema=short), path)
    with newer.transaction() as tx:
        with tx.update_snapshot().fast_append() as append:
            append.append_data_file(parquet_file_to_data_file(newer.io, tx.table_metadata, path))
    pinned = {**attr, "snapshot_id": tables.catalog.load_table("ns.t").current_snapshot().snapshot_id}
    errors = gate_field_ids(_points_root(tmp_path, pinned), tables.catalog)
    assert "points/t: short.parquet lacks fields grid_tile, extra" in errors


def test_field_ids_passes_files_with_the_table_ids_and_a_required_instance(tmp_path):
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    table = pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=SCHEMA)
    _, attr = _write(tables, "t", table)
    assert gate_field_ids(_points_root(tmp_path, attr), tables.catalog) == []


def test_field_ids_fails_a_file_without_ids_and_an_optional_instance(tmp_path):
    catalog = local_catalog(tmp_path / "c")
    catalog.create_namespace("ns")
    loose = pa.schema([("instance", pa.int64()), ("grid_tile", pa.string())])
    path = str(tmp_path / "a.parquet")
    pq.write_table(pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=loose), path)
    ref = register_files(catalog, "ns.t", [path], schema=loose, properties={})
    attr = iceberg_table_attr(ref, catalog="local", org="local", namespace="ns", table="t", index_column="instance")
    errors = gate_field_ids(_points_root(tmp_path, attr), catalog)
    assert errors == [
        "points/t: instance is optional in the table schema",
        "points/t: a.parquet has no field id, or another, for instance, grid_tile",
        "points/t: a.parquet has a nullable instance",
    ]


@pytest.mark.parametrize("with_ids", [True, False])
def test_field_ids_fails_a_file_column_the_table_lacks(tmp_path, with_ids):
    catalog = local_catalog(tmp_path / "c")
    catalog.create_namespace("ns")
    wide = pa.schema([*SCHEMA, ("extra", pa.int64())])
    if with_ids:
        wide = pa.schema([f.with_metadata({b"PARQUET:field_id": str(i).encode()}) for i, f in enumerate(wide, 1)])
    path = str(tmp_path / "wide.parquet")
    pq.write_table(pa.table({"instance": [1], "grid_tile": ["0,0"], "extra": [7]}, schema=wide), path)
    register_files(catalog, "ns.t", [path], schema=wide, properties={})
    # A later schema drops the column, and a later snapshot still holds the wide file.
    with catalog.load_table("ns.t").update_schema() as update:
        update.delete_column("extra")
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    tables.require("t", schema=SCHEMA, properties={})
    added = tmp_path / "added.parquet"
    write_parquet(pa.table({"instance": [2], "grid_tile": ["0,0"]}, schema=SCHEMA), str(added),
                  index_column="instance", field_ids=tables.field_ids("t"))
    attr = table_attr(tables, "t", tables.write_parts("t", [added]))
    errors = gate_field_ids(_points_root(tmp_path, attr), catalog)
    assert "points/t: wide.parquet has columns the table lacks: extra" in errors
    assert all("wide.parquet" in e and str(tmp_path) not in e for e in errors)


def _pinned_fixture(tmp_path):
    """A repo whose ``points/transcripts`` pins a table snapshot that a later append left behind."""
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    tables.require("transcripts", schema=SCHEMA, properties={})
    repo = create_repo(tmp_path / "repo", [], prefixes=[tables.data_prefix("transcripts")])
    ref, attr = _write(tables, "transcripts", pa.table({"instance": [1, 2], "grid_tile": ["0,0"] * 2}, schema=SCHEMA))
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="w")
    root.require_group("shapes")
    group = root.require_group("points").require_group("transcripts")
    group.attrs["iceberg_table"] = attr
    with zarr.config.set({"array.rectilinear_chunks": True}):
        declare_copy(group, "points.parquet", ref.files)
    session.commit("pin")
    tables.write("transcripts", pa.table({"instance": [3], "grid_tile": ["0,1"]}, schema=SCHEMA), replace=True)
    return tables.catalog, zarr.open_group(repo.readonly_session("main").store, mode="r"), attr


def test_tables_copies_and_attrs_read_the_pinned_snapshot(tmp_path):
    catalog, root, attr = _pinned_fixture(tmp_path)
    assert catalog.load_table("ns.transcripts").current_snapshot().snapshot_id != attr["snapshot_id"]
    transcripts = zip_v2_store(tmp_path, "transcripts", {"grid/0,0/location": (np.zeros((2, 3), "float32"), {})})
    cells = zip_v2_store(tmp_path, "cells", {"polygon_sets/0/num_vertices": (np.zeros(5, "int32"), {})})
    assert gate_tables(root, catalog, cells, transcripts) == []
    with zarr.config.set({"array.rectilinear_chunks": True}):
        assert gate_copies(root, catalog) == []
    assert gate_attrs(root, catalog) == []


def test_attrs_fails_a_pin_the_table_lacks(tmp_path):
    catalog, root, attr = _pinned_fixture(tmp_path)
    root = _points_root(tmp_path / "other", {**attr, "snapshot_id": 1})
    assert gate_attrs(root, catalog) == ["points/t: the table has no snapshot 1"]


CELL_IDS = np.array(["aaaaaaab-1", "aaaaaaac-1", "aaaaaaad-1"])
SETS = {"nucleus_boundaries": 0, "cell_boundaries": 1}


def _polygon_root(tmp_path, replacements=None):
    """A root with both polygon elements, each pinning a table of the real polygon sets of ``cells_zip``.

    ``replacements`` maps a table name to a table that is written in place of the set's own.
    """
    zf = cells_zip(tmp_path)
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    root.attrs["metrics"] = {"number_of_nucleus_non_simple_polygons": None}
    shapes = root.require_group("shapes")
    for name, index in SETS.items():
        tables.require(name, **table_specs({}, [])[name])
        table = (replacements or {}).get(name, polygon_table(zf, index, CELL_IDS))
        shapes.require_group(name).attrs["iceberg_table"] = table_attr(
            tables, name, tables.write(name, table), geo={"x": 1})
    root.require_group("points")
    return root, tables, zf


def _summary(nucleus_count):
    return np.array([[c] for c in nucleus_count], dtype="float64"), ["nucleus_count"]


def _with(table, column, values):
    """``table`` with ``column`` replaced by ``values``."""
    index = table.schema.get_field_index(column)
    return table.set_column(index, table.schema.field(column), pa.array(values, type=table.schema.field(column).type))


NUCLEUS = "shapes/nucleus_boundaries"


def test_polygons_passes_both_sets(tmp_path, capsys):
    root, tables, zf = _polygon_root(tmp_path)
    assert gate_polygons(root, tables.catalog, zf, *_summary([1, 0, 2])) == []
    assert "shapes/cell_boundaries" in capsys.readouterr().out


def test_polygons_fails_a_repeated_instance(tmp_path):
    nucleus = polygon_table(cells_zip(tmp_path), 0, CELL_IDS)
    root, tables, zf = _polygon_root(tmp_path / "x", {"nucleus_boundaries": _with(nucleus, "instance", [1, 1])})
    errors = gate_polygons(root, tables.catalog, zf, *_summary([1, 0, 2]))
    assert errors == [f"{NUCLEUS}: 1 instance repeats, first 1"]


def test_polygons_fails_a_part_count_that_cell_summary_contradicts(tmp_path):
    root, tables, zf = _polygon_root(tmp_path)
    errors = gate_polygons(root, tables.catalog, zf, *_summary([1, 1, 2]))
    assert errors == [f"{NUCLEUS}: part_count differs from nucleus_count on 1 cell, first cell_index 1"]


def test_polygons_fails_a_nucleus_count_that_is_nan(tmp_path):
    root, tables, zf = _polygon_root(tmp_path)
    errors = gate_polygons(root, tables.catalog, zf, *_summary([1, np.nan, 2]))
    assert errors == ["cell_summary nucleus_count is NaN on 1 cell, first cell_index 1"]


def test_polygons_fails_a_nucleus_table_that_lacks_a_part(tmp_path):
    table = _with(polygon_table(cells_zip(tmp_path), 0, CELL_IDS), "part_count", [1, 1])
    root, tables, zf = _polygon_root(tmp_path / "x", {"nucleus_boundaries": table})
    assert gate_polygons(root, tables.catalog, zf, *_summary([1, 0, 2])) == [
        f"{NUCLEUS}: part_count sums to 2, the source has 3 rows",
        f"{NUCLEUS}: part_count differs from the source row count on 1 cell, first cell_index 2",
        f"{NUCLEUS}: part_count differs from nucleus_count on 1 cell, first cell_index 2",
    ]


def test_polygons_fails_a_cell_table_that_lacks_a_part(tmp_path):
    table = _with(polygon_table(cells_zip(tmp_path), 1, CELL_IDS), "part_count", [1, 1, 0])
    root, tables, zf = _polygon_root(tmp_path / "x", {"cell_boundaries": table})
    assert gate_polygons(root, tables.catalog, zf, *_summary([1, 0, 2])) == [
        "shapes/cell_boundaries: part_count sums to 2, the source has 3 rows",
        "shapes/cell_boundaries: part_count differs from the source row count on 1 cell, first cell_index 2",
    ]


def test_polygons_reads_the_pinned_snapshot(tmp_path):
    root, tables, zf = _polygon_root(tmp_path)
    # A later snapshot repeats an instance; the pinned one does not.
    tables.write("nucleus_boundaries", polygon_table(zf, 1, CELL_IDS), replace=True)
    assert gate_polygons(root, tables.catalog, zf, *_summary([1, 0, 2])) == []


def test_polygons_fails_a_count_of_invalid_parts_that_the_metrics_contradict(tmp_path):
    root, tables, zf = _polygon_root(tmp_path)
    root.attrs["metrics"] = {"number_of_nucleus_non_simple_polygons": 4}
    errors = gate_polygons(root, tables.catalog, zf, *_summary([1, 0, 2]))
    assert errors == ["0 invalid nucleus polygons, the metrics CSV states 4"]


def test_tables_counts_the_cells_of_a_polygon_set_not_its_rows(tmp_path):
    root, tables, zf = _polygon_root(tmp_path)
    transcripts = zip_v2_store(tmp_path, "transcripts", {"grid/0,0/location": (np.zeros((2, 3), "float32"), {})})
    # The root has no transcripts element, so only the shapes are counted.
    assert gate_tables(root, tables.catalog, zf, transcripts) == []


def _region_root(tmp_path, *, obs_instance, region="cell_boundaries", cells=(0, 1, 2)):
    """A root whose table has ``obs_instance`` and whose cell_boundaries table holds the cells ``cells``."""
    zf = cells_zip(tmp_path)
    full = polygon_table(zf, 1, CELL_IDS)
    root, tables, _ = _polygon_root(tmp_path, {"cell_boundaries": full.take(list(cells))})
    table = root.require_group("tables").require_group("table")
    prefix, suffix = np.arange(1, 4), np.ones(3, dtype=np.int64)
    packed = prefix.astype(np.int64) + suffix * 2**32
    obs = pd.DataFrame({
        "cell_id": packed[: len(obs_instance)], "instance": np.array(obs_instance, dtype=np.int64),
        "region": pd.Categorical([region] * len(obs_instance)),
    }, index=pd.Index(list("abc")[: len(obs_instance)], name="barcode"))
    write_elem(table, "obs", obs)
    write_table_keys(table, region, region_key="region", instance_key="instance")
    return root, tables


def test_table_region_passes_a_table_that_matches_cell_boundaries(tmp_path):
    root, tables = _region_root(tmp_path, obs_instance=[1, 2, 3])
    assert gate_table_region(root, tables.catalog) == []


def test_table_region_fails_a_missing_polygon(tmp_path):
    root, tables = _region_root(tmp_path, obs_instance=[1, 2, 3], cells=(0, 2))
    assert gate_table_region(root, tables.catalog) == ["obs.instance has 1 instance that cell_boundaries lacks, first 2"]


def test_table_region_fails_a_polygon_without_a_row(tmp_path):
    root, tables = _region_root(tmp_path, obs_instance=[1, 2])
    assert gate_table_region(root, tables.catalog) == ["cell_boundaries has 1 instance that obs.instance lacks, first 3"]


def test_table_region_fails_a_cell_id_that_differs(tmp_path):
    root, tables = _region_root(tmp_path, obs_instance=[1, 2, 3])
    obs = root["tables/table/obs/cell_id"]
    obs[:] = obs[:] + 1
    errors = gate_table_region(root, tables.catalog)
    assert len(errors) == 1 and errors[0].startswith("cell_id differs on 3 of 3 sampled instances, first instance 1")


def test_table_region_fails_a_region_other_than_cell_boundaries(tmp_path):
    root, tables = _region_root(tmp_path, obs_instance=[1, 2, 3], region="cell_labels")
    assert gate_table_region(root, tables.catalog) == [
        "tables/table: region attribute is 'cell_labels'",
        "tables/table: uns region is 'cell_labels'",
        "tables/table: obs.region holds 'cell_labels'",
    ]


def test_table_region_fails_a_repeated_obs_instance(tmp_path):
    root, tables = _region_root(tmp_path, obs_instance=[1, 2, 2])
    assert gate_table_region(root, tables.catalog) == [
        "cell_boundaries has 1 instance that obs.instance lacks, first 3",
        "obs.instance has 1 instance that repeats, first 2",
        "cell_id differs on 1 of 3 sampled instances, first instance 2",
    ]


def test_a_gate_that_raises_fails_and_the_run_goes_on(capsys):
    ran = []

    def raises():
        raise KeyError("shapes/cell_boundaries")

    gates = {"a": raises, "b": lambda: ran.append("b") or [], "c": lambda: ["bad"]}
    assert run_gates(gates, list(gates)) == ["a", "c"]
    out = capsys.readouterr().out
    assert "a FAIL" in out and "raised KeyError: 'shapes/cell_boundaries'" in out and "b PASS" in out
    assert ran == ["b"]


def test_select_gates_takes_commas_or_spaces_and_refuses_an_unknown_name():
    available = {"images": 0, "labels": 1, "polygons": 2}
    assert select_gates(None, available) == ["images", "labels", "polygons"]
    assert select_gates(["labels,images", "polygons"], available) == ["labels", "images", "polygons"]
    with pytest.raises(ValueError, match="nope"):
        select_gates(["images,nope"], available)


DIFFEXP = "Feature ID,Feature Name,G Mean Counts\nENSG1,A,0.5\nENSG2,B,2.0\n"


def _codewords_zip(tmp_path, names):
    return zip_v2_store(
        tmp_path, "transcripts", {"codeword_category": (np.zeros((len(names), 9), dtype=bool), {})},
        attrs={"codeword_gene_names": names, "codeword_gene_mapping": list(range(len(names)))},
    )


class _ExtraBundle:
    """The transcripts zip and one CSV for both differential expression members."""

    def __init__(self, transcripts, csv):
        self.transcripts, self.csv = transcripts, csv

    def member_zip(self, name):
        return self.transcripts

    def member_file(self, name):
        return io.BytesIO(self.csv.encode())


def _extra_root(tmp_path, csv=DIFFEXP, names=("A", "B")):
    """A root whose ``iceberg_tables`` attribute pins the tables that ``write_extra_tables`` wrote."""
    from minus80.core.elements import ElementWriter
    from sources import SAMPLES

    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    transcripts = _codewords_zip(tmp_path, list(names))
    write_extra_tables(ElementWriter(repo, "fixture"), _ExtraBundle(transcripts, csv), tables,
                       SAMPLES["kidney"], LAYOUTS, "repo")
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    root.attrs[ICEBERG_TABLES] = dict(zarr.open_group(repo.readonly_session("fixture").store, mode="r").attrs[ICEBERG_TABLES])
    return root, tables, transcripts


def test_extra_tables_pass_when_each_table_equals_its_source(tmp_path):
    root, tables, transcripts = _extra_root(tmp_path)
    assert gate_extra_tables(root, tables.catalog, transcripts, _ExtraBundle(transcripts, DIFFEXP)) == []


def test_extra_tables_fail_a_row_count_that_the_source_contradicts(tmp_path):
    root, tables, transcripts = _extra_root(tmp_path)
    source = _ExtraBundle(_codewords_zip(tmp_path / "y", ["A", "B", "C"]), DIFFEXP + "ENSG3,C,1.0\n")
    errors = gate_extra_tables(root, tables.catalog, source.transcripts, source)
    assert "iceberg_tables/codewords: 2 rows, source 3" in errors
    assert "iceberg_tables/diffexp_azimuth: 2 rows, source 3" in errors


def test_extra_tables_fail_a_repeated_index_value(tmp_path):
    csv = DIFFEXP + "ENSG1,C,1.0\n"
    root, tables, transcripts = _extra_root(tmp_path, csv)
    errors = gate_extra_tables(root, tables.catalog, transcripts, _ExtraBundle(transcripts, csv))
    assert errors == [
        "iceberg_tables/diffexp_azimuth: 1 Feature ID value repeats, first ENSG1",
        "iceberg_tables/diffexp_graphclust: 1 Feature ID value repeats, first ENSG1",
    ]


def test_extra_tables_fail_a_pin_the_table_lacks(tmp_path):
    root, tables, transcripts = _extra_root(tmp_path)
    root.attrs[ICEBERG_TABLES] = {**root.attrs[ICEBERG_TABLES],
                                  "codewords": {**root.attrs[ICEBERG_TABLES]["codewords"], "snapshot_id": 1}}
    errors = gate_extra_tables(root, tables.catalog, transcripts, _ExtraBundle(transcripts, DIFFEXP))
    assert errors == ["iceberg_tables/codewords: the table has no snapshot 1"]


def test_extra_tables_fail_a_file_without_field_ids(tmp_path):
    root, tables, transcripts = _extra_root(tmp_path)
    catalog = tables.catalog
    loose = pa.schema([("Feature ID", pa.string())])
    path = str(tmp_path / "loose.parquet")
    pq.write_table(pa.table({"Feature ID": ["x"]}, schema=loose), path)
    ref = register_files(catalog, "ns.loose", [path], schema=loose, properties={})
    pins = dict(root.attrs[ICEBERG_TABLES])
    pins["codewords"] = iceberg_table_attr(ref, catalog="local", org="local", namespace="ns", table="loose",
                                           index_column="Feature ID")
    root.attrs[ICEBERG_TABLES] = pins
    errors = gate_extra_tables(root, catalog, transcripts, _ExtraBundle(transcripts, DIFFEXP))
    assert any("iceberg_tables/codewords" in e and "no field id" in e for e in errors)


def test_extra_tables_fail_a_root_without_the_attribute(tmp_path):
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    transcripts = _codewords_zip(tmp_path, ["A"])
    assert gate_extra_tables(root, local_catalog(tmp_path / "c"), transcripts, _ExtraBundle(transcripts, DIFFEXP)) == [
        "the root has no iceberg_tables attribute"
    ]


# The full store: a synthetic bundle written by ``ingest.py --mode full``, then verified in full mode.

MASK = np.arange(48, dtype="<u4").reshape(6, 8)
METHODS = ["a", "b", "c", "d", "e"]


def _cells(tmp_path, *, separator=".", masks=(MASK, MASK[::-1])):
    """The cells store of ``cells_zip`` with two masks of 4 chunks each and a one-column ``cell_summary``."""
    encoding = {"name": "v2", "separator": separator}
    mask = {"chunks": (3, 4), "compressors": None, "chunk_key_encoding": encoding}
    return cells_zip(tmp_path, extra={
        "masks/0": (masks[0], mask), "masks/1": (masks[1], mask),
        "cell_summary": (np.array([[1.0], [0.0], [2.0]]), {"attributes": {"column_names": ["nucleus_count"]}}),
    }, attrs={"segmentation_methods": METHODS})


def _inner(zf) -> bytes:
    return Path(zf.filename).read_bytes()


def _bundle_members(tmp_path) -> dict:
    members = image_members(tmp_path)
    members["experiment.spatial"] = json.dumps({
        "pixel_size": PIXEL, "panel_wta": {"panel_organism": "Human"},
        "analysis_sw_version": "1", "instrument_sw_version": "2",
    }).encode()
    members["cells.zarr.zip"] = _inner(_cells(tmp_path))
    members[MATRIX] = _inner(_matrix_zip(tmp_path))
    members["transcripts.zarr.zip"] = _inner(_transcripts_zip(tmp_path))
    members["metrics_summary_x.csv"] = b"number_of_nucleus_non_simple_polygons,other\n,5\n"
    for name in ("azimuth", "graphclust"):
        members[f"diffexp_genes/gene_expression_{name}.csv"] = DIFFEXP.encode()
    bag = _bag()
    bag["density_plot"]["bins"] |= {
        label: f"_resources_{key}" for label, key in (
            ("Negative Control Codewords", "codewords"), ("Negative Control Probes", "probes"),
            ("Genomic Controls", "genomic"))}
    bag["_resources"] |= {key: _resource(DENSITY) for key in ("codewords", "probes", "genomic")}
    members["analysis_summary_x.html"] = _html(bag).encode()
    return members


class Store:
    """A repo and its catalog on disk, and the local objects that the ingest read."""

    def __init__(self, directory: Path, branch: str = "main"):
        self.directory, self.branch = directory, branch
        self.repo, self.catalog = directory / "repo", directory / "catalog"
        self.bundle, self.he, self.alignment = directory / "outs.zip", directory / "he.ome.tif", directory / "al.csv"
        self.record = directory / "records" / "kidney.json"

    def write_inputs(self, change=None) -> None:
        """The synthetic bundle, the H&E and the alignment. Without ``change`` the record of the bundle too.

        ``change(members)`` alters the members first; such a bundle has no record.
        """
        members = _bundle_members(self.directory)
        if change:
            change(members)
        write_bundle(self.bundle, members)
        _rgb_pyramid(self.he, planar=False)
        np.savetxt(self.alignment, ALIGNMENT, delimiter=",")
        if not change:
            found = bundle_facts(Bundle(self.bundle), lambda: self.he.open("rb"), self.alignment.read_text)
            assert write_record("kidney", found, self.record.parent) == []

    def copy(self, directory: Path) -> "Store":
        """The repo and the catalog in ``directory``; the data files stay where they are."""
        shutil.copytree(self.repo, directory / "repo")
        shutil.copytree(self.catalog, directory / "catalog")
        copy = Store(directory, self.branch)
        copy.bundle, copy.he, copy.alignment, copy.record = self.bundle, self.he, self.alignment, self.record
        return copy

    def root(self):
        return verify.open_root(str(self.repo), self.branch)

    def tables(self):
        return local_catalog(self.catalog)

    def bundle_zip(self, name):
        return open_bundle(str(self.bundle))[0].member_zip(name)

    def edit(self, change) -> None:
        """Commit ``change(root)`` on the branch."""
        session = create_repo(self.repo, [], open_existing=True).writable_session(self.branch)
        change(zarr.open_group(session.store, mode="a"))
        commit(session, "edit")

    def argv(self, *args):
        return ["verify.py", "--sample", "kidney", "--repo", str(self.repo), "--catalog", str(self.catalog),
                "--bundle", str(self.bundle), "--he-image", str(self.he), "--he-alignment", str(self.alignment),
                "--record", str(self.record), *args]


@pytest.fixture(scope="module")
def full_store(tmp_path_factory):
    """``ingest.py --mode full`` on a synthetic bundle, onto the branch ``main``. Tests copy it to damage it."""
    store = Store(tmp_path_factory.mktemp("full"))
    store.write_inputs()
    argv = ["ingest.py", "--sample", "kidney", "--repo", str(store.repo), "--catalog", str(store.catalog),
            "--namespace", "ns", "--mode", "full", "--branch", "main", "--bundle", str(store.bundle),
            "--he-image", str(store.he), "--he-alignment", str(store.alignment), "--record", str(store.record),
            "--concurrency", "2"]
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(store.directory)
        patch.setattr(sys, "argv", argv)
        ingest.main()
    return store


@pytest.fixture(autouse=True)
def rectilinear_chunks():
    """A reader of a ``uint8`` copy sets this, as ``verify.py`` does."""
    with zarr.config.set({"array.rectilinear_chunks": True}):
        yield


def _verify(monkeypatch, store, *args):
    monkeypatch.setattr(sys, "argv", store.argv(*args))
    with pytest.raises(SystemExit) as raised:
        verify.main()
    return raised.value.code


ALL_GATES = ("bundle,complete,images,transforms,qc,labels,matrix,tables,polygons,table_region,copies,attrs,"
             "extra_tables,field_ids,table_index,source")


def test_every_gate_of_a_full_store_passes_and_a_local_bundle_skips_the_source_gate(
        monkeypatch, capsys, full_store):
    code = _verify(monkeypatch, full_store, "--mode", "full", "--gates", ALL_GATES)
    out = capsys.readouterr().out
    assert code == 0, out
    for gate in ALL_GATES.split(","):
        if gate != "source":
            assert f"{gate} PASS" in out
    assert "source: skipped" in out and "source PASS" not in out and "source FAIL" not in out
    assert out.index("bundle PASS") < out.index("complete PASS")
    assert out.index("images PASS") < out.index("transforms PASS") < out.index("qc PASS")


@pytest.mark.parametrize("mode", ["fixture", "full"])
def test_the_gate_names_of_each_mode_are_the_gates_that_main_builds(monkeypatch, full_store, mode):
    built = []
    # The recorder stands in for the run of the gates only; main builds the gates itself.
    monkeypatch.setattr(verify, "run_gates", lambda gates, names: built.append(tuple(gates)) or [])
    assert _verify(monkeypatch, full_store, "--mode", mode, "--branch", "main") == 0
    assert built == [verify.GATES[mode]]
    assert verify.GATES["fixture"] == tuple(g for g in verify.GATES["full"] if g not in ("bundle", "complete", "source"))


def test_fixture_mode_runs_the_gates_of_the_fixture_and_knows_no_complete_gate(monkeypatch, capsys, full_store):
    assert _verify(monkeypatch, full_store, "--mode", "fixture", "--branch", "main", "--gates", "attrs,table_index") == 0
    capsys.readouterr()
    refusal = _verify(monkeypatch, full_store, "--mode", "fixture", "--branch", "main", "--gates", "complete")
    assert "unknown gates: complete" in refusal


def test_the_bundle_gate_passes_on_the_bundle_of_the_record(monkeypatch, capsys, full_store):
    assert _verify(monkeypatch, full_store, "--mode", "full", "--gates", "bundle") == 0
    assert "bundle PASS" in capsys.readouterr().out


def test_the_bundle_gate_fails_a_bundle_that_differs_from_the_record(monkeypatch, capsys, tmp_path, full_store):
    other = Store(tmp_path / "other")
    other.directory.mkdir()
    other.write_inputs(lambda members: members.pop("diffexp_genes/gene_expression_azimuth.csv"))
    copy = full_store.copy(tmp_path / "copy")
    copy.bundle = other.bundle
    assert _verify(monkeypatch, copy, "--mode", "full", "--gates", "bundle") == 1
    out = capsys.readouterr().out
    assert "bundle FAIL" in out and "diffexp_genes/gene_expression_azimuth.csv" in out


def test_a_refused_record_fails_the_bundle_gate_with_its_reason(monkeypatch, capsys, tmp_path, full_store):
    copy = full_store.copy(tmp_path / "copy")
    copy.record = tmp_path / "absent" / "kidney.json"
    assert _verify(monkeypatch, copy, "--mode", "full", "--gates", "bundle") == 1
    assert "bundle FAIL" in (out := capsys.readouterr().out) and "no record of kidney" in out


def test_a_full_store_is_complete(full_store):
    assert gate_complete(full_store.root()) == []


def _complete_errors(full_store, tmp_path, change):
    copy = full_store.copy(tmp_path)
    copy.edit(change)
    return gate_complete(copy.root())


def test_complete_fails_an_image_without_the_mark_and_a_label_without_it(full_store, tmp_path):
    def unmark(root):
        root["images/morphology"].attrs[COMPLETE] = False
        del root["labels/cell_labels"].attrs[COMPLETE]

    assert _complete_errors(full_store, tmp_path, unmark) == [
        f"labels/cell_labels: not marked {COMPLETE}", f"images/morphology: not marked {COMPLETE}"]


def test_complete_fails_a_missing_qc_element_and_names_it(full_store, tmp_path):
    errors = _complete_errors(full_store, tmp_path, lambda root: root["images"].__delitem__("qc_genomic_controls"))
    assert errors == ["images/qc_genomic_controls: missing"]


def test_complete_fails_a_missing_iceberg_tables_entry_and_a_missing_attribute(full_store, tmp_path):
    def drop_entry(root):
        pins = dict(root.attrs[ICEBERG_TABLES])
        del pins["codewords"]
        root.attrs[ICEBERG_TABLES] = pins

    assert _complete_errors(full_store, tmp_path, drop_entry) == [f"{ICEBERG_TABLES}/codewords: missing"]
    assert _complete_errors(full_store, tmp_path / "b", lambda root: root.attrs.__delitem__(ICEBERG_TABLES)) == [
        f"{ICEBERG_TABLES}: missing"]


def test_complete_fails_an_element_without_its_table_pin(full_store, tmp_path):
    errors = _complete_errors(full_store, tmp_path,
                              lambda root: root["points/transcripts"].attrs.__delitem__("iceberg_table"))
    assert errors == ["points/transcripts: has no iceberg_table attribute"]


def test_complete_fails_a_missing_list_that_differs_from_what_the_full_mode_leaves_out(full_store, tmp_path):
    def change(root):
        root.attrs["missing"] = [*MISSING_FROM_FULL[1:], {"what": "images/qc_mean_q_score", "why": "x"}]

    errors = _complete_errors(full_store, tmp_path, change)
    assert errors == [f"missing: lacks {MISSING_FROM_FULL[0]['what']}",
                      "missing: lists images/qc_mean_q_score, which the full mode writes"]


def test_the_full_tree_of_the_complete_gate_is_the_elements_that_the_ingest_writes():
    tree = verify.full_elements()
    assert tree == [e for e in ingest.ELEMENTS["full"] if e != "root"]
    assert "images/qc_genomic_controls" in tree and ICEBERG_TABLES in tree


def test_element_tables_of_a_full_store_name_every_missing_element(tmp_path):
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    with pytest.raises(ValueError) as raised:
        element_tables(root, full=True)
    for element in ("shapes/nucleus_boundaries", "shapes/cell_boundaries", "points/transcripts"):
        assert element in str(raised.value)
    assert element_tables(root) == {}


@pytest.mark.parametrize("path", ["images/he_image", "images/morphology_3d", "images/morphology"])
def test_images_fail_on_a_missing_element_in_full_mode_and_skip_it_in_fixture_mode(full_store, tmp_path, path):
    copy = full_store.copy(tmp_path)
    copy.edit(lambda root: root.__delitem__(path))
    bundle = open_bundle(str(copy.bundle))[0]
    assert gate_images(copy.root(), bundle, str(copy.he), full=True) == [f"{path}: missing"]
    assert gate_images(copy.root(), bundle, str(copy.he)) == []


def test_qc_fails_on_a_missing_element_in_full_mode_and_skips_it_in_fixture_mode(full_store, tmp_path):
    copy = full_store.copy(tmp_path)
    copy.edit(lambda root: root["images"].__delitem__("qc_mean_q_score"))
    bundle = open_bundle(str(copy.bundle))[0]
    assert gate_qc(copy.root(), bundle, full=True) == ["images/qc_mean_q_score: missing"]
    assert gate_qc(copy.root(), bundle) == []


def test_images_in_full_mode_fail_a_level_that_the_group_lacks_though_it_lists_the_source_levels(full_store, tmp_path):
    copy = full_store.copy(tmp_path)
    copy.edit(lambda root: root["images/he_image"].__delitem__("s1"))
    bundle = open_bundle(str(copy.bundle))[0]
    errors = gate_images(copy.root(), bundle, str(copy.he), full=True)
    assert errors == ["images/he_image: marked complete, holds levels [0, 2] of 3"]


def _matrix_of(tmp_path, rows):
    """A matrix store whose ``X`` is ``rows`` rows of two values each."""
    source = tmp_path / "other.zarr"
    write_elem(zarr.open_group(source, mode="w", zarr_format=2), "X",
               scipy.sparse.csr_matrix(np.ones((rows, 2), dtype="int32")))
    return zip_directory(source, tmp_path / "other.zip")


def test_matrix_takes_its_shape_and_stored_values_from_the_bundle(full_store, tmp_path):
    assert gate_matrix(full_store.root(), full_store.bundle_zip(MATRIX), full=True) == []
    assert gate_matrix(full_store.root(), full_store.bundle_zip(MATRIX)) == []
    errors = gate_matrix(full_store.root(), _matrix_of(tmp_path, 4), full=True)
    assert errors[0] == "X has shape (3, 2) and 4 stored values; the source has (4, 2) and 8"


# gate_labels

def _label_repo(tmp_path, cells):
    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("work", repo.lookup_branch("main"))
    write_labels(ElementWriter(repo, "work"), _Bundle(cells), PIXEL, concurrency=1)
    return zarr.open_group(repo.readonly_session("work").store, mode="r")


@pytest.mark.parametrize("separator", [".", "/"])
def test_labels_compare_chunks_stored_under_either_separator(tmp_path, separator):
    cells = _cells(tmp_path / "a", separator=separator)
    names = chunk_members(cells, "masks/0")
    assert len(names) == 4
    assert {chunk_key("labels/nucleus_labels/s0", "masks/0", n) for n in names} == {
        f"labels/nucleus_labels/s0/c/{r}/{c}" for r in (0, 1) for c in (0, 1)}
    assert gate_labels(_label_repo(tmp_path / "b", cells), cells) == []


def test_labels_fail_a_chunk_that_differs_from_the_source(tmp_path):
    root = _label_repo(tmp_path / "b", _cells(tmp_path / "a"))
    other = _cells(tmp_path / "c", masks=(MASK + 1, MASK[::-1]))
    errors = gate_labels(root, other)
    assert len(errors) == 4 and all("differs" in e and e.startswith("labels/nucleus_labels/s0") for e in errors)


def test_labels_compare_every_chunk_by_hash(tmp_path, capsys):
    cells = _cells(tmp_path / "a")
    root = _label_repo(tmp_path / "b", cells)
    assert gate_labels(root, cells) == []
    assert "comparing 4 of 4 chunks of masks/0" in capsys.readouterr().out


def test_labels_fail_a_store_that_holds_fewer_chunks_than_the_source(tmp_path):
    cells = _cells(tmp_path / "a")
    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("work", repo.lookup_branch("main"))
    write_labels(ElementWriter(repo, "work"), _Bundle(cells), PIXEL, concurrency=1)
    session = repo.writable_session("work")
    sync(session.store.delete("labels/nucleus_labels/s0/c/0/0"))
    commit(session, "drop a chunk")
    errors = gate_labels(zarr.open_group(repo.readonly_session("work").store, mode="r"), cells)
    assert "labels/nucleus_labels/s0: 3 chunks stored, 4 members" in errors


# gate_tables on the transcripts

GRID = {"grid_keys": list(TILES), "grid_number_objects": list(TILES.values())}


def _held_transcripts(tmp_path, origins):
    """A transcripts table that holds each tile of ``origins`` from its first instance; the pin of its last snapshot."""
    bundle, tables, _, _ = _transcripts_run(tmp_path)
    for tile, first in origins.items():
        _hold(tables, bundle, tmp_path, tile, first_instance=first)
    return bundle, tables, table_attr(tables, "transcripts", tables.ref("transcripts"))


def test_transcripts_pass_when_each_tile_has_its_rows_and_its_place_in_the_source_order(tmp_path, capsys):
    _, tables, attr = _held_transcripts(tmp_path, {"0,0": 0, "-1,0": 5})
    assert verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID) == []
    assert "2 tiles, 6 rows, the source 6" in capsys.readouterr().out


def test_transcripts_fail_a_tile_with_the_wrong_instance_origin(tmp_path):
    _, tables, attr = _held_transcripts(tmp_path, {"0,0": 0, "-1,0": 7})
    errors = verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID)
    assert len(errors) == 3
    assert errors[0] == "points/transcripts: tile -1,0 has instances 7..7, the source order gives 5..5"
    assert "row group 0 has instances 7..7, expected from 5" in errors[1]
    assert errors[2] == "points/transcripts: instance runs to 4 in tile 0,0 and starts at 7 in tile -1,0"


def test_transcripts_fail_a_missing_tile_and_a_total_that_the_source_contradicts(tmp_path):
    _, tables, attr = _held_transcripts(tmp_path, {"0,0": 0})
    assert verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID) == [
        "points/transcripts: tile -1,0 has 0 rows, the source 1", "points/transcripts: 5 rows, the source 6"]


def test_transcripts_fail_a_tile_with_the_wrong_row_count_and_a_tile_the_source_lacks(tmp_path):
    bundle, tables, _, _ = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, rows=3)
    _hold(tables, bundle, tmp_path, "-1,0", first_instance=5, name="9,9")
    attr = table_attr(tables, "transcripts", tables.ref("transcripts"))
    errors = verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID)
    assert "points/transcripts: tile 0,0 has 3 rows, the source 5" in errors
    assert "points/transcripts: tile 9,9 is not in the source" in errors
    assert "points/transcripts: tile -1,0 has 0 rows, the source 1" in errors


def test_transcripts_read_the_pinned_snapshot_not_a_later_one(tmp_path):
    bundle, tables, attr = _held_transcripts(tmp_path, {"0,0": 0, "-1,0": 5})
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, name="extra")
    assert tables.partitions("transcripts") != {"0,0": 5, "-1,0": 1}
    assert verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID) == []


def test_the_tables_gate_of_a_full_store_counts_each_table_against_the_source(full_store):
    transcripts = full_store.bundle_zip("transcripts.zarr.zip")
    cells = full_store.bundle_zip(CELLS)
    assert gate_tables(full_store.root(), full_store.tables(), cells, transcripts, full=True) == []


def test_the_tables_gate_of_a_full_store_fails_a_missing_element(full_store, tmp_path):
    copy = full_store.copy(tmp_path)
    copy.edit(lambda root: root["points"].__delitem__("transcripts"))
    with pytest.raises(ValueError, match="points/transcripts"):
        gate_tables(copy.root(), copy.tables(), copy.bundle_zip(CELLS), copy.bundle_zip("transcripts.zarr.zip"), full=True)


# gate_copies

BIG = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("value", pa.float64())])
ROWS = 1_600_000


def _copy_run(tmp_path, *, chunk=COPY_CHUNK, rows=ROWS):
    """A repo whose ``points/big`` is a copy of the 20 MB data file of the table ``big``, and the table's writer."""
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    tables.require("big", schema=BIG, properties={})
    repo = create_repo(tmp_path / "repo", [], prefixes=[tables.data_prefix("big")])
    data = {"instance": np.arange(rows), "value": np.random.default_rng(0).random(rows)}
    ref = tables.write("big", pa.table(data, schema=BIG))
    session = repo.writable_session("main")
    group = zarr.open_group(session.store, mode="w").require_group("points").require_group("big")
    group.attrs["iceberg_table"] = table_attr(tables, "big", ref)
    declare_copy(group, "points.parquet", ref.files, chunk=chunk)
    commit(session, "copy")
    return tables, repo, ref


def _root(repo):
    return zarr.open_group(repo.readonly_session("main").store, mode="r")


def _repin(repo, attr):
    session = repo.writable_session("main")
    zarr.open_group(session.store, mode="a")["points/big"].attrs["iceberg_table"] = attr
    commit(session, "repin")


def _other_table(tables, name, directory, data: bytes | None = None):
    """The table ``name`` with one data file, the bytes ``data`` or a three-row file; its pin."""
    if data is None:
        tables.require(name, schema=BIG, properties={})
        ref = tables.write(name, pa.table({"instance": np.arange(3), "value": np.zeros(3)}, schema=BIG))
    else:
        file = directory / f"{name}.parquet"
        file.write_bytes(data)
        ref = register_files(tables.catalog, f"ns.{name}", [str(file)], schema=BIG, properties={})
    return iceberg_table_attr(ref, catalog="local", org="local", namespace="ns", table=name, index_column="instance")


def test_copies_pass_by_block_hash_on_a_20_mb_file(tmp_path, capsys):
    tables, repo, ref = _copy_run(tmp_path)
    assert 19_000_000 < ref.files[0].size < 21_000_000
    assert gate_copies(_root(repo), tables.catalog) == []
    out = capsys.readouterr().out
    assert "points/big: 1 uint8 arrays" in out and "comparing 3 blocks" in out


def test_copies_fail_a_changed_byte_in_a_sampled_block(tmp_path):
    tables, repo, ref = _copy_run(tmp_path)
    data = file_path(ref.files[0].location).read_bytes()
    changed = bytearray(data)
    changed[COPY_CHUNK + 100] ^= 1
    _repin(repo, _other_table(tables, "changed", tmp_path, bytes(changed)))
    errors = gate_copies(_root(repo), tables.catalog)
    assert len(errors) == 1 and "1 of 3 sampled blocks differ, first block 1" in errors[0]
    assert "changed.parquet" in errors[0] and str(tmp_path) not in errors[0]


def test_copies_fail_a_length_that_differs_from_the_files_and_an_edge_that_is_not_a_file_boundary(tmp_path):
    tables, repo, _ = _copy_run(tmp_path)
    _repin(repo, _other_table(tables, "small", tmp_path))
    errors = gate_copies(_root(repo), tables.catalog)
    assert any(e.startswith("points/big: the copies have") and "the data files" in e for e in errors)
    assert any("chunk edges" in e for e in errors)


def test_copies_fail_chunk_edges_that_are_not_the_reference_size(tmp_path):
    tables, repo, _ = _copy_run(tmp_path, chunk=4 * 2**20)
    errors = gate_copies(_root(repo), tables.catalog)
    assert len(errors) == 1 and "chunk edges" in errors[0]


def test_copies_fail_a_copy_that_the_catalog_lacks_a_file_for(tmp_path):
    tables, repo, ref = _copy_run(tmp_path)
    session = repo.writable_session("main")
    declare_copy(zarr.open_group(session.store, mode="a")["points/big"], "extra.parquet", ref.files)
    commit(session, "second copy")
    errors = gate_copies(_root(repo), tables.catalog)
    assert errors == ["points/big: 2 uint8 arrays, 1 data files"]


def test_copies_of_a_full_store_pair_the_parts_with_the_files_in_the_source_tile_order(full_store):
    transcripts = full_store.bundle_zip("transcripts.zarr.zip")
    orders = verify.copy_orders(transcripts)
    assert orders == {"points/transcripts": ["0,0", "-1,0"]}
    assert gate_copies(full_store.root(), full_store.tables(), orders, full=True) == []


# gate_source

SAMPLE = SAMPLES["kidney"]


def _layouts(size, etag):
    return ("| Repo | Object | Size | ETag | Last modified |\n|---|---|---|---|---|\n"
            f"| `atera-kidney` | `{SAMPLE.name}_outs.zip` | {size:,} | `{etag}` | 2026-10-15 |\n")


def _object(tmp_path):
    path = tmp_path / "outs.zip"
    path.write_bytes(b"0123456789")
    return path, hashlib.md5(path.read_bytes()).hexdigest()


def _head_of(path, etag=None):
    """The HEAD response of the object at ``url``, from a local file."""
    return lambda url: (path.stat().st_size, etag or hashlib.md5(path.read_bytes()).hexdigest())


def _source_root(tmp_path, layouts):
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    root.attrs["source"] = ingest.source_objects(layouts, SAMPLE)
    return root


def test_source_passes_when_the_head_response_equals_layouts_and_the_root_attribute(tmp_path):
    path, etag = _object(tmp_path)
    layouts = _layouts(10, etag)
    assert gate_source(_source_root(tmp_path, layouts), SAMPLE, layouts, _head_of(path)) == []


def test_source_fails_a_wrong_etag_and_a_wrong_size(tmp_path):
    path, etag = _object(tmp_path)
    layouts = _layouts(10, etag)
    url = SAMPLE.url("_outs.zip")
    errors = gate_source(_source_root(tmp_path, layouts), SAMPLE, layouts, _head_of(path, etag="0" * 32))
    assert errors == [f"{url}: ETag {'0' * 32}, LAYOUTS.md records {etag}"]
    path.write_bytes(b"01234567890")
    errors = gate_source(_source_root(tmp_path / "b", layouts), SAMPLE, layouts, _head_of(path, etag=etag))
    assert errors == [f"{url}: size 11, LAYOUTS.md records 10"]


def test_source_fails_a_root_attribute_that_differs_from_layouts(tmp_path):
    path, etag = _object(tmp_path)
    layouts = _layouts(10, etag)
    root = _source_root(tmp_path, _layouts(10, "f" * 32))
    errors = gate_source(root, SAMPLE, layouts, _head_of(path))
    assert errors == [f"{SAMPLE.url('_outs.zip')}: the root attribute source differs from LAYOUTS.md"]
    root.attrs["source"] = []
    assert gate_source(root, SAMPLE, layouts, _head_of(path)) == [
        f"{SAMPLE.url('_outs.zip')}: the root attribute source differs from LAYOUTS.md"]


def test_the_recorded_objects_of_every_sample_have_a_size_and_an_etag():
    layouts = (Path(verify.__file__).with_name("LAYOUTS.md")).read_text()
    for sample in SAMPLES.values():
        objects = ingest.source_objects(layouts, sample)
        assert len(objects) == 5 and all(o["size"] > 0 and o["etag"] for o in objects)


class _Head(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "10")
        self.send_header("ETag", '"abc-3"')
        self.end_headers()

    def log_message(self, *args):
        pass


def test_http_head_reads_the_size_and_the_unquoted_etag_of_a_head_response():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Head)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert http_head(f"http://127.0.0.1:{server.server_port}/x") == (10, "abc-3")
    finally:
        server.shutdown()
        server.server_close()


def test_a_local_bundle_drops_the_source_gate_with_a_printed_line(capsys):
    assert without_source(["images", "source", "attrs"], local=True) == ["images", "attrs"]
    assert "source: skipped" in capsys.readouterr().out
    assert without_source(["source"], local=False) == ["source"]
    assert capsys.readouterr().out == ""


# The Arraylake org form

def test_a_catalog_and_a_repo_use_the_same_org_form():
    assert catalog_org("arraylake://earthmover-biology") == "earthmover-biology"
    assert catalog_org("/tmp/catalog") is None


@pytest.mark.parametrize("repo, catalog", [("arraylake://o/r", "catalog"), ("repo", "arraylake://o")])
def test_the_ingest_refuses_a_repo_and_a_catalog_that_are_not_both_local_or_both_on_arraylake(
        monkeypatch, tmp_path, repo, catalog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ingest.py", "--sample", "kidney", "--repo", repo, "--catalog", catalog,
                                      "--namespace", "n", "--branch", "work"])
    with pytest.raises(SystemExit, match="both be local or both be on Arraylake"):
        ingest.main()


# gate_matrix compares every copied chunk in full mode

def _chunked_matrix(tmp_path):
    """A 20-row matrix zip whose ``X`` arrays have several chunks each, and a repo that holds a copy of it."""
    source = tmp_path / "m.zarr"
    root = zarr.open_group(source, mode="w", zarr_format=2)
    write_elem(root, "X", scipy.sparse.csr_matrix(np.arange(1, 41, dtype="int32").reshape(20, 2)),
               dataset_kwargs={"chunks": (6,)})
    zf = zip_directory(source, tmp_path / "m.zip")
    repo = create_repo(tmp_path / "repo", [])
    session = repo.writable_session("main")
    copy_group(zf, "", zarr.open_group(session.store, mode="w").require_group("tables").create_group("table"))
    commit(session, "copy")
    return zf, repo


def _flip(repo, key, at=-1):
    """Change one byte of the stored object ``key`` on ``main``."""
    session = repo.writable_session("main")
    data = bytearray(sync(session.store.get(key, default_buffer_prototype())).to_bytes())
    data[at] ^= 1
    sync(session.store.set(key, default_buffer_prototype().buffer.from_bytes(bytes(data))))
    commit(session, "flip")


def _main_root(repo):
    return zarr.open_group(repo.readonly_session("main").store, mode="r")


def test_matrix_in_full_mode_compares_every_chunk_of_every_copied_array(tmp_path, capsys):
    zf, repo = _chunked_matrix(tmp_path)
    assert len(chunk_members(zf, "X/data")) == 7 and len(chunk_members(zf, "X/indptr")) == 4
    assert gate_matrix(_main_root(repo), zf, full=True) == []
    assert "comparing 18 chunks" in capsys.readouterr().out


def test_matrix_in_full_mode_fails_a_changed_indptr(tmp_path):
    zf, repo = _chunked_matrix(tmp_path)
    _flip(repo, "tables/table/X/indptr/c/0")
    assert gate_matrix(_main_root(repo), zf, full=True) == ["X/indptr: chunk X/indptr/0 differs"]


def test_matrix_in_full_mode_fails_a_changed_byte_in_a_late_data_chunk(tmp_path):
    zf, repo = _chunked_matrix(tmp_path)
    _flip(repo, "tables/table/X/data/c/6")
    assert gate_matrix(_main_root(repo), zf, full=True) == ["X/data: chunk X/data/6 differs"]


def test_matrix_in_full_mode_fails_a_missing_chunk_and_changed_metadata(tmp_path):
    zf, repo = _chunked_matrix(tmp_path)
    session = repo.writable_session("main")
    sync(session.store.delete("tables/table/X/data/c/3"))
    root = zarr.open_group(session.store, mode="a")
    root["tables/table/X/indices"].attrs["x"] = 1
    commit(session, "damage")
    errors = gate_matrix(_main_root(repo), zf, full=True)
    assert errors == ["X/data: 6 chunks stored, 7 members", "X/data: chunk X/data/3 differs"]
    other = zip_directory(_matrix_dir(tmp_path / "o", 4), tmp_path / "o.zip")
    assert any("metadata differs" in e for e in gate_matrix(_main_root(repo), other, full=True))


def _matrix_dir(tmp_path, chunk):
    source = tmp_path / "m.zarr"
    write_elem(zarr.open_group(source, mode="w", zarr_format=2), "X",
               scipy.sparse.csr_matrix(np.arange(1, 41, dtype="int32").reshape(20, 2)),
               dataset_kwargs={"chunks": (chunk,)})
    return source


# chunk_members

def test_chunk_members_skip_the_directory_entries_of_a_zip(tmp_path):
    path = tmp_path / "d.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name in ("masks/", "masks/0/", "masks/0/1/", "masks/0/1/0", "masks/0/1/1", "masks/0/.zarray", "masks/0/2.3"):
            zf.writestr(name, b"")
    assert sorted(chunk_members(zipfile.ZipFile(path), "masks/0")) == ["masks/0/1/0", "masks/0/1/1", "masks/0/2.3"]


# gate_masks and gate_instances

CELLS_N = 60
SUMMARY_COLUMNS = ["nucleus_area", "cell_area", "nucleus_centroid_y", "nucleus_centroid_x",
                   "cell_centroid_y", "cell_centroid_x"]


def _grid_store():
    """Cell ``i`` is a 10 by 10 square with a 4 by 4 nucleus; a pixel is 1 micrometre."""
    nucleus, cell = (np.zeros((10 * CELLS_N, 12), dtype="<u4") for _ in range(2))
    for i in range(CELLS_N):
        cell[10 * i:10 * i + 10, 0:10] = i + 1
        nucleus[10 * i + 3:10 * i + 7, 3:7] = i + 1
    root = zarr.group()
    for name, data in (("nucleus_labels", nucleus), ("cell_labels", cell)):
        root.require_group(f"labels/{name}").create_array("s0", data=data)
    obs = root.require_group("tables/table/obs")
    obs.create_array("cell_id", data=np.arange(1, CELLS_N + 1, dtype="int64") + 7 * 2**32)
    obs.create_array("instance", data=np.arange(1, CELLS_N + 1, dtype="int64"))
    summary = np.array([[16, 100, 10 * i + 4.5, 4.5, 10 * i + 5, 5.0] for i in range(CELLS_N)])
    pairs = np.array([[i + 1, 7] for i in range(CELLS_N)], dtype="<u4")
    return root, summary, pairs


def test_masks_pass_when_each_mask_agrees_best_with_its_own_area(capsys):
    root, summary, _ = _grid_store()
    assert verify.gate_masks(root, summary, SUMMARY_COLUMNS, 1.0) == []
    assert "median label area 16.00 um2" in capsys.readouterr().out


def test_masks_fail_when_the_two_masks_are_swapped():
    root, summary, _ = _grid_store()
    nucleus, cell = root["labels/nucleus_labels/s0"], root["labels/cell_labels/s0"]
    data = nucleus[:]
    nucleus[:], cell[:] = cell[:], data
    assert verify.gate_masks(root, summary, SUMMARY_COLUMNS, 1.0) == [
        "nucleus_area agrees better with cell_labels than with nucleus_labels",
        "cell_area agrees better with nucleus_labels than with cell_labels"]


def test_instances_pass_when_the_mask_the_obs_ids_and_the_instance_agree(capsys):
    root, summary, pairs = _grid_store()
    assert verify.gate_instances(root, summary, SUMMARY_COLUMNS, pairs, 1.0) == []
    assert "50 rows, 0 mismatches" in capsys.readouterr().out


def test_instances_fail_an_instance_that_is_not_the_row_plus_one():
    root, summary, pairs = _grid_store()
    root["tables/table/obs/instance"][:] = np.arange(2, CELLS_N + 2)
    errors = verify.gate_instances(root, summary, SUMMARY_COLUMNS, pairs, 1.0)
    assert len(errors) == 50 and all("obs.instance is" in e for e in errors)
    root["tables/table/obs/instance"][:] = np.arange(1, CELLS_N + 1)
    root["tables/table/obs/cell_id"][:] = 0
    assert all("obs.cell_id" in e for e in verify.gate_instances(root, summary, SUMMARY_COLUMNS, pairs, 1.0))


# Locations

@pytest.mark.parametrize("repo, catalog", [("repo", "arraylake:org"), ("arraylake://o/r", "cat"), ("repo", "arraylake://o")])
def test_verify_refuses_a_repo_and_a_catalog_that_are_not_both_local_or_both_on_arraylake_and_makes_no_directory(
        monkeypatch, tmp_path, repo, catalog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify.py", "--sample", "kidney", "--repo", repo, "--catalog", catalog])
    with pytest.raises(SystemExit):
        verify.main()
    assert list(tmp_path.iterdir()) == []


def test_verify_requires_a_sample(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "argv", ["verify.py", "--repo", str(tmp_path / "r"), "--catalog", str(tmp_path / "c")])
    with pytest.raises(SystemExit) as raised:
        verify.main()
    assert raised.value.code == 2 and "--sample" in capsys.readouterr().err


# gate_tables: row groups of each transcripts file

def _hold_instances(tables, bundle, tmp_path, instances):
    """Register tile ``0,0`` with ``instances`` in row groups of two rows."""
    from minus80.core.tables import write_parquet
    from tables_extra import codewords_table
    from transcripts import tile_table

    table = tile_table(bundle.zf, "0,0", codewords_table(bundle.zf), first_instance=0)
    table = table.set_column(0, table.schema.field("instance"), pa.array(instances, pa.int64()))
    path = tmp_path / "groups.parquet"
    write_parquet(table, str(path), index_column="instance", field_ids=tables.field_ids("transcripts"), row_group_size=2)
    tables.write_parts("transcripts", [path])
    return table_attr(tables, "transcripts", tables.ref("transcripts"))


GRID_ONE = {"grid_keys": ["0,0"], "grid_number_objects": [5]}


def test_row_groups_chain_when_each_starts_after_the_last_and_the_first_starts_at_the_tile_origin(tmp_path):
    bundle, tables, _, _ = _transcripts_run(tmp_path)
    attr = _hold_instances(tables, bundle, tmp_path, [0, 1, 2, 3, 4])
    assert verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID_ONE) == []


def test_row_groups_fail_a_repeat_that_the_manifest_bounds_do_not_show(tmp_path):
    bundle, tables, _, _ = _transcripts_run(tmp_path)
    attr = _hold_instances(tables, bundle, tmp_path, [0, 1, 1, 3, 4])
    errors = verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID_ONE)
    assert errors and all("row group 1 has instances 1..3, expected from 2" in e for e in errors)
    assert not any(str(tmp_path) in e for e in errors)


def test_row_groups_fail_a_gap_between_row_groups(tmp_path):
    bundle, tables, _, _ = _transcripts_run(tmp_path)
    attr = _hold_instances(tables, bundle, tmp_path, [0, 1, 3, 3, 4])
    errors = verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID_ONE)
    assert any(e.endswith(".parquet row group 1 has instances 3..3, expected from 2") for e in errors)


def test_row_groups_fail_a_first_row_group_that_does_not_start_at_the_tile_origin(tmp_path):
    bundle, tables, _, _ = _transcripts_run(tmp_path)
    attr = _hold_instances(tables, bundle, tmp_path, [1, 2, 3, 4, 5])
    errors = verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID_ONE)
    assert any("row group 0 has instances 1..2, expected from 0" in e for e in errors)


# gate_copies pairs each file with its own copy

def _two_file_copy(tmp_path):
    """A table of two tiles whose data files have the same size, and a repo with one copy of each in tile order."""
    from minus80.core.tables import write_parquet

    schema = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string()),
                        ("value", pa.float64())])
    tables = TableWriter(local_catalog(tmp_path / "c"), "ns", catalog_name="local", org="local")
    tables.require("two", schema=schema, properties={}, partition_by="grid_tile")
    repo = create_repo(tmp_path / "repo", [], prefixes=[tables.data_prefix("two")])
    for n, tile in enumerate("ab"):
        rows = np.arange(10_000 + n * 1000, 11_000 + n * 1000)
        table = pa.table({"instance": rows, "grid_tile": [tile] * 1000,
                          "value": np.full(1000, n + 0.5)}, schema=schema)
        path = tmp_path / f"{tile}.parquet"
        write_parquet(table, str(path), index_column="instance", field_ids=tables.field_ids("two"))
        tables.write_parts("two", [path])
    ref = tables.ref("two", partition_order=["a", "b"])
    assert ref.files[0].size == ref.files[1].size
    session = repo.writable_session("main")
    group = zarr.open_group(session.store, mode="w").require_group("points").require_group("two")
    group.attrs["iceberg_table"] = table_attr(tables, "two", ref)
    declare_copy(group, "points.parquet", ref.files)
    commit(session, "copy")
    return tables, repo


def test_copies_fail_two_files_of_equal_size_in_the_wrong_order_and_pass_in_the_right_one(tmp_path):
    tables, repo = _two_file_copy(tmp_path)
    assert gate_copies(_root(repo), tables.catalog, {"points/two": ["a", "b"]}) == []
    errors = gate_copies(_root(repo), tables.catalog, {"points/two": ["b", "a"]})
    assert len(errors) == 2 and all("1 of 1 sampled blocks differ, first block 0" in e for e in errors)
    assert all(str(tmp_path) not in e for e in errors)


def test_row_groups_fail_when_they_are_out_of_file_order(tmp_path):
    bundle, tables, _, _ = _transcripts_run(tmp_path)
    attr = _hold_instances(tables, bundle, tmp_path, [2, 3, 0, 1, 4])
    errors = verify._transcript_errors("points/transcripts", tables.catalog, attr, GRID_ONE)
    assert any("row group 0 has instances 2..3, expected from 0" in e for e in errors)


# A second ingest on a finished store, and the stops that end a run with one line.

def _ingest(monkeypatch, store, *extra):
    argv = ["ingest.py", "--sample", "kidney", "--repo", str(store.repo), "--catalog", str(store.catalog),
            "--namespace", "ns", "--mode", "full", "--branch", "main", "--bundle", str(store.bundle),
            "--he-image", str(store.he), "--he-alignment", str(store.alignment), "--record", str(store.record),
            "--concurrency", "2", *extra]
    monkeypatch.chdir(store.directory)
    monkeypatch.setattr(sys, "argv", argv)
    ingest.main()


def _store_state(store):
    """The repo's commits on the branch and each table's snapshots."""
    from minus80.core.icechunk_io import open_repository

    catalog = store.tables()
    return ([s.id for s in open_repository(store.repo).ancestry(branch=store.branch)],
            {str(t): [s.snapshot_id for s in catalog.load_table(t).metadata.snapshots] for t in catalog.list_tables("ns")})


@pytest.mark.parametrize("flag", [(), ("--replace-tables", ",".join(ingest.REPLACEABLE))])
def test_a_second_full_run_on_a_finished_store_changes_nothing(monkeypatch, capsys, full_store, flag):
    before = _store_state(full_store)
    capsys.readouterr()
    _ingest(monkeypatch, full_store, *flag)
    out = capsys.readouterr().out
    assert _store_state(full_store) == before
    assert "root attributes: on branch main already, equal, skipped" in out
    for name in (flag and ingest.REPLACEABLE) or ():
        element = ingest.REPLACE_ELEMENTS[name]
        assert f"--replace-tables {name}: {element} is on branch main already; held, not replaced" in out


def test_a_root_attribute_that_differs_is_written_again(monkeypatch, capsys, full_store, tmp_path):
    copy = full_store.copy(tmp_path)
    copy.edit(lambda root: root.attrs.update({"title": "other"}))
    before = _store_state(copy)
    _ingest(monkeypatch, copy)
    after = _store_state(copy)
    assert copy.root().attrs["title"] != "other"
    assert len(after[0]) == len(before[0]) + 1 and after[1] == before[1]


def _without(store, path):
    """Remove the element ``path`` from the branch, so a rerun writes it."""
    parent, name = path.split("/")
    store.edit(lambda root: root[parent].__delitem__(name))


def test_a_refused_write_ends_the_run_with_one_line_that_names_the_flag(monkeypatch, full_store, tmp_path):
    copy = full_store.copy(tmp_path)
    _without(copy, "shapes/nucleus_boundaries")
    real = ingest.polygon_table
    monkeypatch.setattr(ingest, "polygon_table", lambda *a, **k: real(*a, **k).slice(0, 1))
    with pytest.raises(SystemExit) as stop:
        _ingest(monkeypatch, copy)
    assert stop.value.code == "ns.nucleus_boundaries holds 1 data file of other content; use --replace-tables nucleus_boundaries"


def test_a_schema_difference_without_the_flag_ends_the_run_with_one_line(monkeypatch, full_store, tmp_path):
    copy = full_store.copy(tmp_path)
    wide = pa.schema([*ingest.POLYGON_SCHEMA, ("extra", pa.int64())])
    TableWriter(copy.tables(), "ns", catalog_name="local", org="local").require(
        "nucleus_boundaries", schema=wide, properties={}, evolve=True)
    with pytest.raises(SystemExit) as stop:
        _ingest(monkeypatch, copy)
    assert str(stop.value.code).startswith("ns.nucleus_boundaries differs from the wanted schema")
    assert "\n" not in str(stop.value.code)


def test_a_held_tile_fault_without_the_flag_ends_the_run_with_the_list_of_faults(monkeypatch, full_store, tmp_path):
    from types import SimpleNamespace

    copy = full_store.copy(tmp_path)
    _without(copy, "points/transcripts")
    tables = TableWriter(copy.tables(), "ns", catalog_name="local", org="local")
    tables.require("transcripts", **table_specs({}, [])["transcripts"])
    zf = copy.bundle_zip("transcripts.zarr.zip")
    _hold(tables, SimpleNamespace(zf=zf), tmp_path, "0,0", first_instance=0, name="9,9")
    with pytest.raises(SystemExit) as stop:
        _ingest(monkeypatch, copy)
    assert "tiles outside the plan: 9,9" in str(stop.value.code)


# gate_transforms

def _axes(*names):
    return {"axes": [{"name": n} for n in names]}


def test_apply_transform_resolves_a_scale_and_a_translation_by_axis_name():
    point = {"c": 0.0, "y": 3.0, "x": 5.0}
    assert apply_transform({"type": "scale", "scale": [1.0, 2.0, 4.0]}, ("c", "y", "x"), point) == {
        "c": 0.0, "y": 6.0, "x": 20.0}
    named = {"type": "translation", "translation": [7.0, -1.0], "input": _axes("x", "y")}
    assert apply_transform(named, ("c", "y", "x"), point) == {"c": 0.0, "y": 2.0, "x": 12.0}


def test_apply_transform_resolves_an_affine_by_its_input_and_output_axes_and_a_sequence_in_order():
    turn = {"type": "affine", "affine": [[0.0, -1.0, 40.0], [1.0, 0.0, 12.5]],
            "input": _axes("x", "y"), "output": _axes("x", "y")}
    point = {"c": 0.0, "y": 3.0, "x": 5.0}
    assert apply_transform(turn, ("c", "y", "x"), point) == {"c": 0.0, "x": 37.0, "y": 17.5}
    chain = {"type": "sequence", "transformations": [turn, {"type": "scale", "scale": [2.0, 10.0], "input": _axes("x", "y")}]}
    assert apply_transform(chain, ("c", "y", "x"), point) == {"c": 0.0, "x": 74.0, "y": 175.0}


def test_apply_transform_refuses_a_type_that_it_does_not_know():
    with pytest.raises(ValueError, match="rotation"):
        apply_transform({"type": "rotation"}, ("y", "x"), {"y": 0.0, "x": 0.0})


#: The kidney's alignment object as LAYOUTS.md records it: 123 bytes, CR LF line ends, no final newline.
KIDNEY_ALIGNMENT = ("0.008566778149855193,1.288853807979006,914.1373717471324\r\n"
                    "-1.288853807979006,0.008566778149855193,32040.595506861275\r\n0,0,1")
KIDNEY_PIXEL = 0.21249222


@pytest.mark.parametrize("pixel, micrometres", [((0, 0), (194.247, 6808.377)), ((23628, 0), (237.259, 337.344))])
def test_the_kidney_alignment_places_the_he_image_as_layouts_records(pixel, micrometres):
    assert len(KIDNEY_ALIGNMENT) == 123
    matrix = verify.parse_alignment(KIDNEY_ALIGNMENT)
    assert matrix[2] == [0.0, 0.0, 1.0]
    expected = verify.aligned(matrix, KIDNEY_PIXEL, {"x": pixel[0], "y": pixel[1]})
    assert (expected["x"], expected["y"]) == pytest.approx(micrometres, abs=1e-3)
    # The stored form of the same transform, written here by hand, gives the same place.
    stored = {"type": "sequence", "transformations": [
        {"type": "affine", "affine": matrix[:2], "input": _axes("x", "y"), "output": _axes("x", "y")},
        {"type": "scale", "scale": [KIDNEY_PIXEL] * 2, "input": _axes("x", "y")}]}
    found = apply_transform(stored, ("c", "y", "x"), {"c": 0.0, "y": pixel[1], "x": pixel[0]})
    assert (found["x"], found["y"]) == pytest.approx(micrometres, abs=1e-3)


@pytest.mark.parametrize("text", ["1,2,3\n4,5,6\n", "1,2\n3,4\n0,0,1\n", "1,2,3\n4,5,6\n0,0,1\n7,8,9\n"])
def test_an_alignment_that_is_not_three_lines_of_three_numbers_is_refused(text):
    with pytest.raises(ValueError, match="3 by 3"):
        verify.parse_alignment(text)


def test_an_alignment_whose_third_row_is_not_0_0_1_is_refused():
    with pytest.raises(ValueError, match=r"third row \[0.0, 0.0, 2.0\], not 0, 0, 1"):
        verify.parse_alignment("1,2,3\n4,5,6\n0,0,2\n")


def _transforms(store, *, full=True):
    bundle = open_bundle(str(store.bundle))[0]
    return gate_transforms(store.root(), bundle, *he_readers(str(store.he), str(store.alignment)), full=full)


def _damaged(full_store, tmp_path, path, change, key="ome"):
    """The transform errors of a copy whose attribute ``key`` of ``path`` went through ``change``."""
    def edit(root):
        value = json.loads(json.dumps(root[path].attrs[key]))
        change(value["multiscales"][0] if key == "ome" else value)
        root[path].attrs[key] = value

    copy = full_store.copy(tmp_path)
    copy.edit(edit)
    return _transforms(copy)


def _levels(errors):
    """The text before the first colon of each error: the element and its level."""
    return {e.split(":")[0] for e in errors}


def test_transforms_pass_on_a_full_store_in_both_modes(full_store, capsys):
    assert _transforms(full_store) == []
    out = capsys.readouterr().out
    for path, levels in (("images/morphology", 3), ("images/morphology_3d", 3), ("images/he_image", 3),
                         ("labels/cell_labels", 1), ("images/qc_mean_q_score_80um", 1)):
        assert f"  transforms: {path}: {levels} level{'s' * (levels > 1)}, axes " in out
    assert "  transforms: images/morphology_3d: 3 levels, axes c, z, y, x, the shape and the corners of each" in out
    assert "  transforms: labels/cell_labels: 1 level, axes y, x, the shape and the corners of each" in out
    assert _transforms(full_store, full=False) == []


def _scale_last(multiscale):
    multiscale["coordinateTransformations"][0]["scale"][-1] *= 1.01


def test_transforms_fail_an_image_scale_that_is_one_percent_larger_at_every_level(full_store, tmp_path):
    errors = _damaged(full_store, tmp_path, "images/morphology", _scale_last)
    assert _levels(errors) == {f"images/morphology s{k}" for k in range(3)}
    assert errors[:2] == [
        "images/morphology s0: corner (c 0, y 0, x 2100) maps to x 450.7125, the source gives 446.25",
        "images/morphology s0: corner (c 0, y 1300, x 2100) maps to x 450.7125, the source gives 446.25"]
    assert len(errors) == 6


def test_transforms_fail_one_level_whose_own_scale_differs_and_say_which(full_store, tmp_path):
    def change(multiscale):
        multiscale["datasets"][1]["coordinateTransformations"][0]["scale"][1] *= 1.01

    errors = _damaged(full_store, tmp_path, "images/morphology", change)
    assert [e.split(" maps to ")[0] for e in errors] == [
        "images/morphology s1: corner (c 0, y 650, x 0)", "images/morphology s1: corner (c 0, y 650, x 1050)"]
    assert all(" maps to y " in e for e in errors)


def test_transforms_fail_a_label_scale_that_differs(full_store, tmp_path):
    errors = _damaged(full_store, tmp_path, "labels/cell_labels", _scale_last)
    assert [e.split(" maps to ")[0] for e in errors] == [
        "labels/cell_labels s0: corner (y 0, x 8)", "labels/cell_labels s0: corner (y 6, x 8)"]
    assert all(" maps to x " in e for e in errors)


def test_transforms_fail_a_stack_whose_z_scale_is_not_the_physical_size_z(full_store, tmp_path):
    def change(multiscale):
        multiscale["coordinateTransformations"][0]["scale"][1] = 3.0

    errors = _damaged(full_store, tmp_path, "images/morphology_3d", change)
    assert _levels(errors) == {f"images/morphology_3d s{k}" for k in range(3)}
    assert errors[0] == "images/morphology_3d s0: corner (c 0, z 3, y 0, x 0) maps to z 9.0, the source gives 7.5"
    # Each of the four corners with the last plane fails, at each level.
    assert len(errors) == 12 and all("z 3, " in e for e in errors)


def test_transforms_fail_an_he_affine_with_x_and_y_swapped(full_store, tmp_path):
    def change(multiscale):
        step = multiscale["coordinateTransformations"][0]["transformations"][0]
        step["affine"] = [[b, a, shift] for a, b, shift in step["affine"]]

    errors = _damaged(full_store, tmp_path, "images/he_image", change)
    assert _levels(errors) == {f"images/he_image s{k}" for k in range(3)}
    assert all("maps to x " in e or "maps to y " in e for e in errors)
    # The first corner has no x or y to swap, so each level fails at its other three corners only.
    assert not any("y 0, x 0" in e for e in errors)


def test_transforms_fail_an_he_affine_that_differs_only_off_the_diagonal_of_the_image(full_store, tmp_path):
    def change(multiscale):
        row = multiscale["coordinateTransformations"][0]["transformations"][0]["affine"][0]
        # The added part is 0 at (x 2100, y 1300), so the first corner and the last keep their places.
        row[0] += 1e-3 * 1300
        row[1] -= 1e-3 * 2100

    errors = _damaged(full_store, tmp_path, "images/he_image", change)
    assert [e.split(" maps to ")[0] for e in errors] == [
        f"images/he_image s{k}: corner (c 0, y {y}, x {x})"
        for k in range(3) for y, x in ((0, 2100 >> k), (1300 >> k, 0))]
    assert all(" maps to x " in e for e in errors)


def test_transforms_fail_an_alignment_attribute_that_differs_from_the_csv(full_store, tmp_path):
    def matrix(alignment):
        alignment["matrix"][0][2] += 1e-6

    errors = _damaged(full_store, tmp_path / "a", "images/he_image", matrix, key="alignment")
    assert len(errors) == 1 and errors[0].startswith("images/he_image: alignment matrix ")
    errors = _damaged(full_store, tmp_path / "b", "images/he_image",
                      lambda alignment: alignment.update(axes=["y", "x"]), key="alignment")
    assert errors == ["images/he_image: alignment axes ['y', 'x'], the CSV is in x, y order"]


def test_transforms_fail_a_qc_translation_that_is_off_by_one_bin(full_store, tmp_path):
    def change(multiscale):
        scale, shift = multiscale["coordinateTransformations"][0]["transformations"]
        shift["translation"][2] += scale["scale"][2]

    errors = _damaged(full_store, tmp_path, "images/qc_mean_q_score", change)
    assert errors == [
        "images/qc_mean_q_score s0: corner (c 0, y 0, x 0) maps to x 10.0, the source gives -10.0",
        "images/qc_mean_q_score s0: corner (c 0, y 0, x 6) maps to x 130.0, the source gives 110.0",
        "images/qc_mean_q_score s0: corner (c 0, y 4, x 0) maps to x 10.0, the source gives -10.0",
        "images/qc_mean_q_score s0: corner (c 0, y 4, x 6) maps to x 130.0, the source gives 110.0",
    ]


def test_transforms_fail_a_qc_scale_that_is_not_the_bin_size(full_store, tmp_path):
    def change(multiscale):
        multiscale["coordinateTransformations"][0]["transformations"][0]["scale"][1] = 40.0

    errors = _damaged(full_store, tmp_path, "images/qc_mean_q_score_80um", change)
    assert errors == [
        "images/qc_mean_q_score_80um s0: corner (c 0, y 1, x 0) maps to y 20.0, the source gives 60.0",
        "images/qc_mean_q_score_80um s0: corner (c 0, y 1, x 2) maps to y 20.0, the source gives 60.0"]


def test_transforms_take_the_bin_size_of_the_coarse_raster_from_its_key_in_the_bag(full_store, tmp_path):
    def change(members):
        bag = verify.data_bag(members["analysis_summary_x.html"].decode())
        bag["density_plot"]["mean_q_score_40um"] = bag["density_plot"].pop("mean_q_score_80um")
        members["analysis_summary_x.html"] = _html(bag).encode()

    assert verify.coarse_bins({"mean_q_score_80um": "_resources_a", "bins": {}, "horizontal_bin_count": 6}) == {
        "mean_q_score_80um": 80.0}
    other = Store(tmp_path)
    other.write_inputs(change)
    errors = gate_transforms(full_store.root(), open_bundle(str(other.bundle))[0],
                             *he_readers(str(full_store.he), str(full_store.alignment)), full=True)
    # The bag of this bundle has no 80 um key, so the source gives the fine grid for the element of that name.
    assert errors[0] == "images/qc_mean_q_score_80um s0: shape (1, 1, 2), the source has (1, 4, 6)"
    assert _levels(errors) == {"images/qc_mean_q_score_80um s0"}


def test_transforms_fail_an_element_with_no_transform_into_global(full_store, tmp_path):
    def change(multiscale):
        multiscale["coordinateTransformations"][0]["output"]["name"] = "elsewhere"

    errors = _damaged(full_store, tmp_path, "labels/nucleus_labels", change)
    assert errors == ["labels/nucleus_labels: a transform into elsewhere, the layout has only global",
                      "labels/nucleus_labels: 0 transforms into global"]


def test_transforms_fail_a_second_transform_into_another_system(full_store, tmp_path):
    def change(multiscale):
        other = json.loads(json.dumps(multiscale["coordinateTransformations"][0]))
        other["output"]["name"] = "aligned"
        multiscale["coordinateTransformations"].append(other)

    errors = _damaged(full_store, tmp_path, "images/morphology", change)
    assert errors == ["images/morphology: a transform into aligned, the layout has only global"]


def test_transforms_fail_a_sequence_whose_last_step_does_not_end_in_global(full_store, tmp_path):
    def change(multiscale):
        multiscale["coordinateTransformations"][0]["transformations"][-1]["output"]["name"] = "elsewhere"

    errors = _damaged(full_store, tmp_path, "images/he_image", change)
    assert errors == ["images/he_image: the last step of the sequence ends in elsewhere, not in global"]


def test_transforms_fail_a_sequence_whose_step_does_not_start_where_the_step_before_it_ends(full_store, tmp_path):
    def change(multiscale):
        step = multiscale["coordinateTransformations"][0]["transformations"][0]
        step["output"]["axes"] = step["output"]["axes"][::-1]

    errors = _damaged(full_store, tmp_path, "images/he_image", change)
    assert errors[0] == ("images/he_image: step 0 of the sequence ends in morphology_pixel (y, x), "
                         "step 1 starts in morphology_pixel (x, y)")


def test_transforms_fail_axes_that_are_not_in_the_order_of_the_source(full_store, tmp_path):
    def change(multiscale):
        multiscale["axes"] = [multiscale["axes"][i] for i in (0, 2, 1)]

    errors = _damaged(full_store, tmp_path, "images/morphology", change)
    assert errors == ["images/morphology: axes c, x, y, the source order is c, y, x"]


def test_transforms_fail_a_level_whose_array_has_the_shape_of_the_source_transposed(full_store, tmp_path):
    copy = full_store.copy(tmp_path)
    copy.edit(lambda root: root.create_array("images/morphology/s2", shape=(4, 525, 325), dtype="u2", overwrite=True))
    assert _transforms(copy) == ["images/morphology s2: shape (4, 525, 325), the source has (4, 325, 525)"]


@pytest.mark.parametrize("full", [True, False])
def test_transforms_fail_an_element_that_lists_no_level_in_both_modes(full_store, tmp_path, full):
    copy = full_store.copy(tmp_path)

    def change(root):
        ome = json.loads(json.dumps(root["labels/cell_labels"].attrs["ome"]))
        ome["multiscales"][0]["datasets"] = []
        root["labels/cell_labels"].attrs["ome"] = ome

    copy.edit(change)
    assert _transforms(copy, full=full) == ["labels/cell_labels: lists no level"]


def _plane(scales):
    """A ``(c, y, x)`` element in memory with the levels (1, 5, 8) and (1, 3, 4), and ``scales`` on the second."""
    root = zarr.group()
    for level, shape in enumerate(((1, 5, 8), (1, 3, 4))):
        root.create_array(f"e/s{level}", shape=shape, dtype="u1")
    system = _axes("c", "y", "x")
    root["e"].attrs["ome"] = {"multiscales": [{
        "axes": system["axes"],
        "datasets": [{"path": "s0", "coordinateTransformations": [{"type": "scale", "scale": [1.0, 1.0, 1.0]}]},
                     {"path": "s1", "coordinateTransformations": [{"type": "scale", "scale": scales}]}],
        "coordinateTransformations": [{"type": "scale", "scale": [1.0, 0.5, 0.5], "input": {"name": "cyx", **system},
                                       "output": {"name": "global", **system}}]}]}
    return verify._placement_errors(root, "e", "cyx", [(1, 5, 8), (1, 3, 4)],
                                    lambda corner: {a: n * 0.5 for a, n in corner.items()})


def test_placement_fails_a_level_with_an_odd_side_whose_y_and_x_scales_are_swapped():
    assert _plane([1.0, 5 / 3, 2.0]) == []
    errors = _plane([1.0, 2.0, 5 / 3])
    assert [e.split(" maps to ")[0] for e in errors] == [
        "e s1: corner (c 0, y 0, x 4)", "e s1: corner (c 0, y 3, x 0)",
        "e s1: corner (c 0, y 3, x 4)", "e s1: corner (c 0, y 3, x 4)"]


@pytest.mark.parametrize("path", ["images/morphology", "images/morphology_3d", "images/he_image",
                                  "labels/cell_labels", "images/qc_genomic_controls"])
def test_transforms_fail_on_a_missing_element_in_full_mode_and_skip_it_in_fixture_mode(
        full_store, tmp_path, capsys, path):
    copy = full_store.copy(tmp_path)
    copy.edit(lambda root: root.__delitem__(path))
    assert _transforms(copy) == [f"{path}: missing"]
    assert "skipped" not in capsys.readouterr().out
    assert _transforms(copy, full=False) == []
    assert f"  transforms: {path}: not held, skipped\n" in capsys.readouterr().out


def test_transforms_pass_a_store_that_holds_one_coarse_level_and_the_labels_only(full_store, tmp_path, capsys):
    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    bundle = open_bundle(str(full_store.bundle))[0]
    writer = ElementWriter(repo, "fixture")
    ingest.write_morphology(writer, bundle, PIXEL, levels=[1], concurrency=1)
    write_labels(writer, bundle, PIXEL, concurrency=1)
    root = zarr.open_group(repo.readonly_session("fixture").store, mode="r")

    def no_object():
        raise AssertionError("a store without the H&E reads no H&E object")

    assert gate_transforms(root, bundle, no_object, no_object, full=False) == []
    out = capsys.readouterr().out
    assert "  transforms: images/morphology: 1 level, axes c, y, x, the shape and the corners of each" in out
    assert "  transforms: images/he_image: not held, skipped" in out
    errors = gate_transforms(root, bundle, no_object, no_object, full=True)
    assert "images/he_image: missing" in errors and "images/morphology_3d: missing" in errors


def test_the_transforms_gate_runs_in_fixture_mode_and_fails_the_run_on_a_changed_scale(
        monkeypatch, capsys, full_store, tmp_path):
    assert _verify(monkeypatch, full_store, "--mode", "fixture", "--branch", "main", "--gates", "transforms") == 0
    assert "transforms PASS" in capsys.readouterr().out
    copy = full_store.copy(tmp_path)

    def change(root):
        ome = json.loads(json.dumps(root["images/morphology"].attrs["ome"]))
        _scale_last(ome["multiscales"][0])
        root["images/morphology"].attrs["ome"] = ome

    copy.edit(change)
    assert _verify(monkeypatch, copy, "--mode", "fixture", "--branch", "main", "--gates", "transforms") == 1
    out = capsys.readouterr().out
    assert "transforms FAIL" in out and "  images/morphology s2: corner (c 0, y 325, x 525) maps to x " in out


def test_a_passed_verification_prints_the_snapshot_that_it_read_and_writes_it_to_the_file(
        monkeypatch, capsys, full_store, tmp_path):
    snapshot = create_repo(full_store.repo, [], open_existing=True).lookup_branch("main")
    path = tmp_path / "snapshot"
    assert _verify(monkeypatch, full_store, "--mode", "full", "--gates", "complete", "--snapshot-file", str(path)) == 0
    assert f"branch main: snapshot {snapshot}\n" in capsys.readouterr().out
    assert path.read_text() == f"{snapshot}\n"
    assert _verify(monkeypatch, full_store, "--mode", "full", "--gates", "complete") == 0
    assert f"branch main: snapshot {snapshot}\n" in capsys.readouterr().out


def test_a_failed_verification_writes_no_snapshot_file(monkeypatch, capsys, full_store, tmp_path):
    copy = full_store.copy(tmp_path)

    def change(root):
        ome = json.loads(json.dumps(root["images/morphology"].attrs["ome"]))
        _scale_last(ome["multiscales"][0])
        root["images/morphology"].attrs["ome"] = ome

    copy.edit(change)
    path = tmp_path / "snapshot"
    assert _verify(monkeypatch, copy, "--mode", "full", "--gates", "complete,transforms", "--snapshot-file", str(path)) == 1
    assert "transforms FAIL" in capsys.readouterr().out and not path.exists()


def test_a_verification_that_ran_no_gate_writes_no_snapshot_file(monkeypatch, capsys, full_store, tmp_path):
    path = tmp_path / "snapshot"
    # A local bundle without --head-source skips the source gate, so no gate runs.
    assert _verify(monkeypatch, full_store, "--mode", "full", "--gates", "source", "--snapshot-file", str(path)) == 0
    assert "source: skipped" in capsys.readouterr().out and not path.exists()


@pytest.mark.parametrize("gates", [",", ",,"])
def test_a_gate_list_with_no_name_is_refused(monkeypatch, full_store, tmp_path, gates):
    path = tmp_path / "snapshot"
    code = _verify(monkeypatch, full_store, "--mode", "full", "--gates", gates, "--snapshot-file", str(path))
    assert code == "--gates gives no gate"
    assert not path.exists()


def test_a_full_store_describes_each_kind_of_element(full_store):
    description = full_store.root().attrs["description"]
    assert all(sentence in description for sentence in KIND_SENTENCES.values())


def test_complete_fails_a_description_that_lacks_the_sentence_of_a_held_kind(full_store, tmp_path):
    def describe_the_fixture(root):
        root.attrs["description"] = describe({element_kind(e) for e in ingest.ELEMENTS["fixture"]})

    assert _complete_errors(full_store, tmp_path, describe_the_fixture) == [
        "description: lacks the sentence for images/morphology_3d",
        "description: lacks the sentence for images/he_image",
        "description: lacks the sentence for images/qc",
        "description: lacks the sentence for iceberg_tables",
    ]
