import json
import os

import icechunk
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zarr
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.io.pyarrow import PyArrowFileIO

from minus80.core.icechunk_io import create_repo
from minus80.core.tables import (
    DataFile,
    TableRef,
    data_file,
    declare_copy,
    declare_file,
    iceberg_table_attr,
    parquet_field_ids,
    register_files,
    require_table,
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


def _data_file(path):
    return data_file(PyArrowFileIO(), path.as_uri())


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
    assert [(f.location, f.size) for f in ref.files] == [(p1, os.path.getsize(p1)), (p2, os.path.getsize(p2))]
    for f in ref.files:
        # The time is the file's, rounded up to a whole second.
        stated = f.last_modified.timestamp()
        assert stated == int(stated) and 0 <= stated - os.path.getmtime(f.location) < 1


def test_register_files_skips_a_file_the_table_already_holds(tmp_path):
    table = _table()
    path = str(tmp_path / "a.parquet")
    write_parquet(table, path, index_column="instance")
    catalog = _catalog(tmp_path)
    first = register_files(catalog, "ns.t", [path], schema=table.schema, properties={})
    again = register_files(catalog, "ns.t", [path], schema=table.schema, properties={})
    assert again == first
    assert catalog.load_table("ns.t").scan().to_arrow().num_rows == table.num_rows


_BBOX = pa.struct([("xmin", pa.float64()), ("xmax", pa.float64())])
_NESTED = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("bbox", _BBOX)])


def _nested():
    return pa.table({"instance": [1, 2], "bbox": [{"xmin": 0.0, "xmax": 1.0}] * 2}, schema=_NESTED)


def _parquet_ids(path) -> dict[str, int | None]:
    return parquet_field_ids(pq.read_schema(path))


def test_parquet_field_ids_gives_none_for_a_column_without_an_id(tmp_path):
    path = str(tmp_path / "a.parquet")
    pq.write_table(_nested(), path)
    assert _parquet_ids(path) == {"instance": None, "bbox": None, "bbox.xmin": None, "bbox.xmax": None}


def test_write_parquet_gives_every_column_the_table_field_id(tmp_path):
    from pyiceberg.schema import index_by_name

    catalog = _catalog(tmp_path)
    table = catalog.create_table("ns.t", schema=_NESTED)
    ids = index_by_name(table.schema())
    path = str(tmp_path / "a.parquet")
    write_parquet(_nested(), path, index_column="instance", field_ids=ids)
    assert _parquet_ids(path) == ids
    assert not pq.read_schema(path).field("instance").nullable
    assert json.loads(pq.read_schema(path).metadata[b"pandas"])["index_columns"] == ["instance"]
    register_files(catalog, "ns.t", [path], schema=_NESTED, properties={})
    assert catalog.load_table("ns.t").scan().to_arrow()["instance"].to_pylist() == [1, 2]


def test_write_parquet_refuses_a_column_without_a_field_id(tmp_path):
    with pytest.raises(KeyError, match="bbox.xmax"):
        write_parquet(_nested(), str(tmp_path / "a.parquet"), index_column="instance",
                      field_ids={"instance": 1, "bbox": 2, "bbox.xmin": 3})


@pytest.mark.parametrize("type_", [pa.list_(pa.int64()), pa.map_(pa.string(), pa.int64())])
def test_write_parquet_refuses_list_and_map_columns(tmp_path, type_):
    table = pa.table({"v": pa.array([None], type_)})
    with pytest.raises(NotImplementedError, match="v"):
        write_parquet(table, str(tmp_path / "a.parquet"), index_column="v", field_ids={"v": 1})


def test_a_new_table_makes_a_non_nullable_column_required(tmp_path):
    table = require_table(_catalog(tmp_path), "ns.t", schema=_NESTED, properties={})
    assert table.schema().find_field("instance").required


def test_an_existing_table_makes_a_non_nullable_column_required(tmp_path):
    catalog = _catalog(tmp_path)
    loose = pa.schema([("instance", pa.int64()), ("bbox", _BBOX)])
    created = catalog.create_table("ns.t", schema=loose)
    table = require_table(catalog, "ns.t", schema=_NESTED, properties={})
    assert table.schema().find_field("instance").required
    assert not table.schema().find_field("bbox").required
    assert str(table.metadata.table_uuid) == str(created.metadata.table_uuid)


def _loose_table_with(tmp_path, instance, properties=None):
    """A table with an optional ``instance`` that holds one file of ``instance`` values."""
    catalog = _catalog(tmp_path)
    loose = pa.schema([("instance", pa.int64()), ("bbox", _BBOX)])
    path = str(tmp_path / "loose.parquet")
    pq.write_table(pa.table({"instance": instance, "bbox": [None] * len(instance)}, schema=loose), path)
    register_files(catalog, "ns.t", [path], schema=loose, properties=properties or {})
    return catalog


def test_an_existing_table_with_a_null_refuses_the_required_column(tmp_path):
    catalog = _loose_table_with(tmp_path, [1, None, None])
    with pytest.raises(ValueError, match=r"ns\.t.*instance.*2 null"):
        require_table(catalog, "ns.t", schema=_NESTED, properties={})
    assert not catalog.load_table("ns.t").schema().find_field("instance").required


def test_an_existing_table_without_null_counts_refuses_the_required_column(tmp_path):
    catalog = _loose_table_with(tmp_path, [1, 2], {"write.metadata.metrics.default": "none"})
    with pytest.raises(ValueError, match=r"ns\.t.*instance.*no null count"):
        require_table(catalog, "ns.t", schema=_NESTED, properties={})


def test_an_existing_table_without_the_column_is_refused(tmp_path):
    catalog = _loose_table_with(tmp_path, [1])
    strict = pa.schema([pa.field("other", pa.int64(), nullable=False)])
    with pytest.raises(ValueError, match=r"ns\.t has no column other"):
        require_table(catalog, "ns.t", schema=strict, properties={})


def test_register_files_with_replace_swaps_the_files_in_one_snapshot(tmp_path):
    catalog = _catalog(tmp_path)
    old, new = str(tmp_path / "old.parquet"), str(tmp_path / "new.parquet")
    write_parquet(_table(), old, index_column="instance")
    first = register_files(catalog, "ns.t", [old], schema=_table().schema, properties={})
    write_parquet(pa.table({"instance": [7], "grid_tile": [0]}), new, index_column="instance")
    ref = register_files(catalog, "ns.t", [new], schema=_table().schema, properties={}, replace=True)
    table = catalog.load_table("ns.t")
    assert ref.table_uuid == first.table_uuid and ref.snapshot_id != first.snapshot_id
    assert [t.file.file_path for t in table.scan().plan_files()] == [new]
    assert table.scan().to_arrow()["instance"].to_pylist() == [7]
    assert table.current_snapshot().summary.operation.value == "overwrite"
    assert table.current_snapshot().parent_snapshot_id == first.snapshot_id
    assert os.path.exists(old)


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
    assert attr["catalog"] == "arraylake"


def test_iceberg_table_attr_names_the_catalog_it_is_given():
    attr = iceberg_table_attr(
        TableRef("u", 7, []), catalog="local", org="o", namespace="n", table="t", index_column="instance",
    )
    assert attr["catalog"] == "local"


def test_declare_copy_reads_back_the_file_bytes(tmp_path):
    # 3 chunks of 8 MiB: the last holds 3 222 785 bytes.
    path = tmp_path / "shapes.parquet"
    path.write_bytes(np.random.default_rng(0).bytes(20_000_001))
    root = _group(tmp_path, path)
    (array,) = declare_copy(root, "shapes/cells/shapes.parquet", [_data_file(path)])
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
    (array,) = declare_copy(root, "s.parquet", [_data_file(path)])
    assert array.metadata.chunk_grid.chunk_shapes == ((3,),)
    assert bytes(array[:]) == b"abc"


def test_several_files_become_parts(tmp_path):
    p1, p2 = tmp_path / "a.parquet", tmp_path / "b.parquet"
    p1.write_bytes(b"x" * 10)
    p2.write_bytes(b"y" * 20)
    root = _group(tmp_path, p1, p2)
    arrays = declare_copy(
        root, "points/t/points.parquet", [_data_file(p1), _data_file(p2)]
    )
    assert [a.basename for a in arrays] == ["part.0.parquet", "part.1.parquet"]
    assert bytes(arrays[1][:]) == b"y" * 20


def test_a_copy_of_a_file_that_changed_afterwards_fails_to_read(tmp_path):
    path = tmp_path / "s.parquet"
    path.write_bytes(b"abc")
    repo = create_repo(tmp_path / "repo", [path.as_uri()])
    session = repo.writable_session("main")
    declare_copy(zarr.open_group(session.store, mode="w"), "s.parquet", [_data_file(path)])
    session.commit("copy")
    path.write_bytes(b"xyz")
    later = os.path.getmtime(path) + 2
    os.utime(path, (later, later))
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    with pytest.raises(icechunk.StorageError, match="checksum"):
        root["s.parquet"][:]


def test_a_file_outside_every_container_is_refused(tmp_path):
    root = _group(tmp_path, tmp_path / "f.parquet")
    with pytest.raises(ValueError, match="container"):
        declare_copy(root, "x", [DataFile("file:///nowhere/f.parquet", 10, None)])


def test_declare_file_refuses_a_wrong_array(tmp_path):
    root = zarr.group()
    with pytest.raises(ValueError, match="uint8"):
        declare_file(root.create_array("a", shape=(4,), dtype="int16"), "a")
    with pytest.raises(ValueError, match="1-D"):
        declare_file(root.create_array("b", shape=(2, 2), dtype="uint8"), "b")
    with pytest.raises(ValueError, match="node name"):
        declare_file(root.create_array("c", shape=(4,), dtype="uint8"), "other")
