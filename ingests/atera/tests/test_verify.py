import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import zarr
from anndata.io import write_elem

from ingest import Tables, local_catalog, write_local
from minus80.core.tables import iceberg_table_attr, register_files
from verify import gate_field_ids, gate_table_index

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


def _points_root(tmp_path, attr):
    root = zarr.open_group(tmp_path / "r.zarr", mode="w")
    root.require_group("shapes")
    root.require_group("points").require_group("t").attrs["iceberg_table"] = attr
    return root


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
