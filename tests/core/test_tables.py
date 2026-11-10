import json
import os

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zarr
from pyiceberg.catalog.sql import SqlCatalog

from minus80.core.icechunk_io import create_repo
from minus80.core.tables import (
    TableRef,
    declare_copy,
    declare_file,
    iceberg_table_attr,
    register_files,
    write_parquet,
)

GEO = {
    "version": "1.1.0",
    "primary_column": "geometry",
    "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Polygon"]}},
}


@pytest.fixture(autouse=True)
def rectilinear_chunks():
    with zarr.config.set({"array.rectilinear_chunks": True}):
        yield


def _table():
    return pa.table({"instance": [1, 2, 3], "grid_tile": [0, 0, 0]})


def _catalog(tmp_path):
    catalog = SqlCatalog(
        "local", uri=f"sqlite:///{tmp_path}/catalog.db", warehouse=f"file://{tmp_path}"
    )
    catalog.create_namespace("ns")
    return catalog


def _group(tmp_path, *paths):
    repo = create_repo(tmp_path / "repo", [p.as_uri() for p in paths])
    return zarr.open_group(repo.writable_session("main").store, mode="w")


def test_write_parquet_puts_geo_and_the_index_in_the_footer(tmp_path):
    path = str(tmp_path / "t.parquet")
    size = write_parquet(_table(), path, index_column="instance", geo=GEO)
    meta = pq.read_schema(path).metadata
    assert json.loads(meta[b"geo"])["primary_column"] == "geometry"
    assert json.loads(meta[b"pandas"])["index_columns"] == ["instance"]
    assert size == os.path.getsize(path)


def test_write_parquet_without_geo_has_no_geo_key(tmp_path):
    path = str(tmp_path / "t.parquet")
    write_parquet(_table(), path, index_column="instance")
    assert b"geo" not in pq.read_schema(path).metadata


def test_register_files_adds_the_files_and_returns_the_snapshot(tmp_path):
    table = _table()
    p1, p2 = str(tmp_path / "a.parquet"), str(tmp_path / "b.parquet")
    for p in (p1, p2):
        write_parquet(table, p, index_column="instance", geo=GEO)
    catalog = _catalog(tmp_path)
    ref = register_files(
        catalog, "ns.t", [p1, p2], schema=table.schema,
        properties={"geo": "{}"}, partition_by="grid_tile",
    )
    loaded = catalog.load_table("ns.t")
    assert str(loaded.metadata.table_uuid) == ref.table_uuid
    assert loaded.current_snapshot().snapshot_id == ref.snapshot_id
    assert loaded.scan().to_arrow().num_rows == 2 * table.num_rows
    assert loaded.properties["geo"] == "{}"
    assert ref.files == [(p1, os.path.getsize(p1)), (p2, os.path.getsize(p2))]


def test_iceberg_table_attr_has_the_contract_keys():
    ref = TableRef("u", 7, [])
    attr = iceberg_table_attr(
        ref, org="o", namespace="n", table="t",
        index_column="instance", geometry_column="geometry",
    )
    assert list(attr) == [
        "catalog", "org", "namespace", "table", "table_uuid", "snapshot_id",
        "index_column", "geometry_column",
    ]
    bare = iceberg_table_attr(ref, org="o", namespace="n", table="t", index_column="instance")
    assert "geometry_column" not in bare


def test_declare_copy_reads_back_the_file_bytes(tmp_path):
    # 3 chunks of 8 MiB: the last holds 3 222 785 bytes.
    path = tmp_path / "shapes.parquet"
    path.write_bytes(np.random.default_rng(0).bytes(20_000_001))
    root = _group(tmp_path, path)
    (array,) = declare_copy(root, "shapes/cells/shapes.parquet", [(path.as_uri(), 20_000_001)])
    assert array.shape == (20_000_001,)
    assert array.metadata.chunk_grid.chunk_shapes == ((8 << 20, 8 << 20, 3_222_785),)
    assert array.dtype == np.uint8
    assert array.attrs["file:name"] == "shapes.parquet"
    assert bytes(array[-5:]) == path.read_bytes()[-5:]
    assert bytes(array[8 << 20 : (8 << 20) + 5]) == path.read_bytes()[8 << 20 : (8 << 20) + 5]


def test_a_file_smaller_than_a_chunk_has_one_chunk(tmp_path):
    path = tmp_path / "s.parquet"
    path.write_bytes(b"abc")
    root = _group(tmp_path, path)
    (array,) = declare_copy(root, "s.parquet", [(path.as_uri(), 3)])
    assert array.metadata.chunk_grid.chunk_shapes == ((3,),)
    assert bytes(array[:]) == b"abc"


def test_several_files_become_parts(tmp_path):
    p1, p2 = tmp_path / "a.parquet", tmp_path / "b.parquet"
    p1.write_bytes(b"x" * 10)
    p2.write_bytes(b"y" * 20)
    root = _group(tmp_path, p1, p2)
    arrays = declare_copy(
        root, "points/t/points.parquet", [(p1.as_uri(), 10), (p2.as_uri(), 20)]
    )
    assert [a.basename for a in arrays] == ["part.0.parquet", "part.1.parquet"]
    assert bytes(arrays[1][:]) == b"y" * 20


def test_a_file_outside_every_container_is_refused(tmp_path):
    root = _group(tmp_path, tmp_path / "f.parquet")
    with pytest.raises(ValueError, match="container"):
        declare_copy(root, "x", [("file:///nowhere/f.parquet", 10)])


def test_declare_file_refuses_a_wrong_array(tmp_path):
    root = zarr.group()
    with pytest.raises(ValueError, match="uint8"):
        declare_file(root.create_array("a", shape=(4,), dtype="int16"), "a")
    with pytest.raises(ValueError, match="1-D"):
        declare_file(root.create_array("b", shape=(2, 2), dtype="uint8"), "b")
    with pytest.raises(ValueError, match="node name"):
        declare_file(root.create_array("c", shape=(4,), dtype="uint8"), "other")
