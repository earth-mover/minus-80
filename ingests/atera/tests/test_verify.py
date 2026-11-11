import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zarr
from anndata.io import write_elem
from pyiceberg.io.pyarrow import parquet_file_to_data_file
from pyiceberg.types import LongType

from ingest import Tables, local_catalog, write_local
from minus80.core.icechunk_io import create_repo
from minus80.core.tables import declare_copy, iceberg_table_attr, register_files
from tests.stores import zip_v2_store
from verify import gate_attrs, gate_copies, gate_field_ids, gate_table_index, gate_tables

SCHEMA = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string())])


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
    tables = Tables(local_catalog(tmp_path / "c"), "ns", kind="local", org="local", write_file=write_local)
    _, attr = tables.write("t", pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=SCHEMA),
                           schema=SCHEMA, properties={})
    iceberg = tables.catalog.load_table("ns.t")
    iceberg.delete()
    empty = tables.catalog.load_table("ns.t").current_snapshot().snapshot_id
    errors = gate_field_ids(_points_root(tmp_path, {**attr, "snapshot_id": empty}), tables.catalog)
    assert errors == [f"points/t: snapshot {empty} has no data files"]


def test_field_ids_checks_the_pinned_snapshot_and_every_field(tmp_path):
    tables = Tables(local_catalog(tmp_path / "c"), "ns", kind="local", org="local", write_file=write_local)
    table = pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=SCHEMA)
    _, attr = tables.write("t", table, schema=SCHEMA, properties={})
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
    tables = Tables(local_catalog(tmp_path / "c"), "ns", kind="local", org="local", write_file=write_local)
    table = pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=SCHEMA)
    _, attr = tables.write("t", table, schema=SCHEMA, properties={})
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
    tables = Tables(catalog, "ns", kind="local", org="local", write_file=write_local)
    _, attr = tables.write("t", pa.table({"instance": [2], "grid_tile": ["0,0"]}, schema=SCHEMA),
                           schema=SCHEMA, properties={})
    errors = gate_field_ids(_points_root(tmp_path, attr), catalog)
    assert f"points/t: {path} has columns the table lacks: extra" in errors
    assert all(path in e for e in errors)


def _pinned_fixture(tmp_path):
    """A repo whose ``points/transcripts`` pins a table snapshot that a later append left behind."""
    tables = Tables(local_catalog(tmp_path / "c"), "ns", kind="local", org="local", write_file=write_local)
    spec = {"schema": SCHEMA, "properties": {}}
    repo = create_repo(tmp_path / "repo", [], prefixes=[tables.data_prefix("transcripts", **spec)])
    ref, attr = tables.write("transcripts", pa.table({"instance": [1, 2], "grid_tile": ["0,0"] * 2}, schema=SCHEMA),
                             **spec)
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="w")
    root.require_group("shapes")
    group = root.require_group("points").require_group("transcripts")
    group.attrs["iceberg_table"] = attr
    with zarr.config.set({"array.rectilinear_chunks": True}):
        declare_copy(group, "points.parquet", ref.files)
    session.commit("pin")
    tables.write("transcripts", pa.table({"instance": [3], "grid_tile": ["0,1"]}, schema=SCHEMA), **spec)
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
