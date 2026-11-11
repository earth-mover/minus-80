import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zarr
from anndata.io import write_elem
from pyiceberg.io.pyarrow import parquet_file_to_data_file
from pyiceberg.types import LongType

from minus80.core.icechunk_io import create_repo
from minus80.ome.spatialdata import write_table_keys
from minus80.core.tables import TableWriter, declare_copy, iceberg_table_attr, local_catalog, register_files
from tests.stores import cells_zip, zip_v2_store
from ingest import table_attr, table_specs
from polygons import polygon_table
from verify import (
    gate_attrs, gate_copies, gate_field_ids, gate_polygons, gate_table_index, gate_table_region, gate_tables,
)

SCHEMA = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string())])


def _write(tables, name, table):
    """``table`` written into table ``name``, required with ``SCHEMA``; the reference and the element attribute."""
    tables.require(name, schema=SCHEMA, properties={})
    ref = tables.write(name, table)
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
    assert f"points/t: {path} lacks fields grid_tile, extra" in errors


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
        f"points/t: {path} has no field id, or another, for instance, grid_tile",
        f"points/t: {path} has a nullable instance",
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
    _, attr = _write(tables, "t", pa.table({"instance": [2], "grid_tile": ["0,0"]}, schema=SCHEMA))
    errors = gate_field_ids(_points_root(tmp_path, attr), catalog)
    assert f"points/t: {path} has columns the table lacks: extra" in errors
    assert all(path in e for e in errors)


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
    tables.write("transcripts", pa.table({"instance": [3], "grid_tile": ["0,1"]}, schema=SCHEMA))
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


def test_polygons_passes_both_sets(tmp_path, capsys):
    root, tables, _ = _polygon_root(tmp_path)
    assert gate_polygons(root, tables.catalog, *_summary([1, 0, 2])) == []
    assert "shapes/cell_boundaries" in capsys.readouterr().out


def test_polygons_fails_a_repeated_instance(tmp_path):
    zf = cells_zip(tmp_path)
    nucleus = polygon_table(zf, 0, CELL_IDS)
    repeated = pa.concat_tables([nucleus, nucleus.slice(0, 1)])
    root, tables, _ = _polygon_root(tmp_path / "x", {"nucleus_boundaries": repeated})
    errors = gate_polygons(root, tables.catalog, *_summary([1, 0, 2]))
    assert errors == ["shapes/nucleus_boundaries: 1 instance repeats, first 1"]


def test_polygons_fails_a_part_count_that_cell_summary_contradicts(tmp_path):
    root, tables, _ = _polygon_root(tmp_path)
    errors = gate_polygons(root, tables.catalog, *_summary([1, 1, 2]))
    assert errors == ["shapes/nucleus_boundaries: part_count differs from nucleus_count on 1 of 3 sampled cells, first cell_index 1"]


def test_polygons_reads_the_pinned_snapshot(tmp_path):
    root, tables, zf = _polygon_root(tmp_path)
    # A later snapshot repeats an instance; the pinned one does not.
    tables.write("nucleus_boundaries", polygon_table(zf, 1, CELL_IDS))
    assert gate_polygons(root, tables.catalog, *_summary([1, 0, 2])) == []


def test_polygons_fails_a_count_of_invalid_parts_that_the_metrics_contradict(tmp_path):
    root, tables, _ = _polygon_root(tmp_path)
    root.attrs["metrics"] = {"number_of_nucleus_non_simple_polygons": 4}
    errors = gate_polygons(root, tables.catalog, *_summary([1, 0, 2]))
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
