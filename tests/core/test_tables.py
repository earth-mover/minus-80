import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import icechunk
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import zarr
from pyarrow.fs import LocalFileSystem, SubTreeFileSystem
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.schema import index_by_name

from minus80.core.icechunk_io import create_repo
from minus80.core.registry import file_path
from minus80.core.tables import (
    DataFile,
    HeldFilesError,
    TableRef,
    TableWriter,
    UPLOAD_ATTEMPTS,
    _write_local,
    COPY_CHUNK,
    copy_edges,
    data_file,
    declare_copy,
    declare_file,
    iceberg_table_attr,
    local_catalog,
    parquet_field_ids,
    partition_bounds,
    partition_counts,
    partition_files,
    parquet_schema,
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


def test_parquet_schema_carries_the_field_ids_and_the_footer_metadata(tmp_path):
    ids = {"instance": 1, "bbox": 2, "bbox.xmin": 3, "bbox.xmax": 4}
    schema = parquet_schema(_NESTED, index_column="instance", geo=GEO, field_ids=ids)
    assert parquet_field_ids(schema) == ids
    assert json.loads(schema.metadata[b"pandas"])["index_columns"] == ["instance"]
    assert json.loads(schema.metadata[b"geo"])["primary_column"] == "geometry"
    path = str(tmp_path / "a.parquet")
    pq.write_table(_nested().cast(schema), path)
    assert pq.read_schema(path).equals(schema, check_metadata=True)


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


def test_data_file_refuses_a_file_io_that_states_no_time(tmp_path):
    from pyiceberg.io.fsspec import FsspecFileIO

    path = tmp_path / "s.parquet"
    path.write_bytes(b"abc")
    with pytest.raises(ValueError, match=f"no last-modified time for {path.as_uri()}"):
        data_file(FsspecFileIO({}), path.as_uri())


def test_declare_copy_refuses_a_file_without_a_time(tmp_path):
    path = tmp_path / "s.parquet"
    path.write_bytes(b"abc")
    root = _group(tmp_path, path)
    with pytest.raises(ValueError, match="no checksum"):
        declare_copy(root, "s.parquet", [DataFile(path.as_uri(), 3, None)])


def test_data_file_names_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.parquet"):
        data_file(PyArrowFileIO(), (tmp_path / "missing.parquet").as_uri())


def test_a_file_outside_every_container_is_refused(tmp_path):
    root = _group(tmp_path, tmp_path / "f.parquet")
    with pytest.raises(ValueError, match="container"):
        declare_copy(root, "x", [DataFile("file:///nowhere/f.parquet", 10, datetime.now(timezone.utc))])


def test_declare_file_refuses_a_wrong_array(tmp_path):
    root = zarr.group()
    with pytest.raises(ValueError, match="uint8"):
        declare_file(root.create_array("a", shape=(4,), dtype="int16"), "a")
    with pytest.raises(ValueError, match="1-D"):
        declare_file(root.create_array("b", shape=(2, 2), dtype="uint8"), "b")
    with pytest.raises(ValueError, match="node name"):
        declare_file(root.create_array("c", shape=(4,), dtype="uint8"), "other")


WRITER_SCHEMA = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string())])
WRITER_SPEC = {"schema": WRITER_SCHEMA, "properties": {"p": "v"}}


class BucketFileIO(PyArrowFileIO):
    """An ``s3://`` FileIO over a local directory: a remote location that needs no network."""

    def _initialize_s3_fs(self, netloc):
        return SubTreeFileSystem(self.properties["bucket-root"], LocalFileSystem())

    def new_output(self, location):
        # A bucket has no directories to create.
        output = super().new_output(location)
        output._filesystem.create_dir(Path(output._path).parent.as_posix(), recursive=True)
        return output


class _ShortOutput:
    """An output file whose stream keeps all but the last byte of each block written to a data file."""

    def __init__(self, output, short):
        self._output, self._short = output, short

    def create(self, overwrite=False):
        stream = self._output.create(overwrite=overwrite)
        if not self._short:
            return stream
        return _ShortStream(stream)

    def __getattr__(self, name):
        return getattr(self._output, name)


class _ShortStream:
    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        return self._stream.write(data[:-1])

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)


class ShortBucketFileIO(BucketFileIO):
    """A bucket whose storage keeps fewer bytes than a data file writes to it."""

    def new_output(self, location):
        return _ShortOutput(super().new_output(location), "/data/" in location)


def _rows(*instances):
    return pa.table({"instance": pa.array(instances, pa.int64()), "grid_tile": ["0,0"] * len(instances)},
                    schema=WRITER_SCHEMA)


def _writer(tmp_path, **spec):
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    writer.require("t", **(WRITER_SPEC | spec))
    return writer


def _remote_writer(tmp_path, io_class=BucketFileIO, **spec):
    root = tmp_path / "bucket"
    (root / "bucket").mkdir(parents=True)
    catalog = SqlCatalog("remote", uri=f"sqlite:///{tmp_path}/catalog.db", warehouse="s3://bucket/wh",
                         **{"py-io-impl": f"{__name__}.{io_class.__name__}", "bucket-root": str(root)})
    writer = TableWriter(catalog, "ns", catalog_name="remote", org="remote")
    writer.require("t", **(WRITER_SPEC | spec))
    return writer, root / "bucket"


def test_local_catalog_keeps_its_warehouse_in_its_directory(tmp_path):
    catalog = local_catalog(tmp_path / "a b")
    catalog.create_namespace("ns")
    table = catalog.create_table("ns.t", schema=WRITER_SCHEMA)
    assert table.location().startswith(f"file://{(tmp_path / 'a b').resolve()}/")


def test_write_gives_the_file_the_table_field_ids_and_a_required_instance(tmp_path):
    writer = _writer(tmp_path)
    ref = writer.write("t", _rows(1, 2))
    iceberg = writer.catalog.load_table("ns.t")
    footer = pq.read_schema(file_path(ref.files[0].location))
    assert {f.name: int(f.metadata[b"PARQUET:field_id"]) for f in footer} == index_by_name(iceberg.schema())
    assert not footer.field("instance").nullable
    assert iceberg.schema().find_field("instance").required


def test_field_ids_gives_the_table_field_id_of_each_column(tmp_path):
    writer = _writer(tmp_path)
    assert writer.field_ids("t") == index_by_name(writer.catalog.load_table("ns.t").schema())
    assert set(writer.field_ids("t")) == {"instance", "grid_tile"}


def test_write_puts_geo_in_the_footer(tmp_path):
    writer = _writer(tmp_path)
    ref = writer.write("t", _rows(1), geo=GEO)
    assert json.loads(pq.read_schema(file_path(ref.files[0].location)).metadata[b"geo"]) == GEO


def test_write_with_replace_swaps_the_file_and_keeps_the_table(tmp_path):
    writer = _writer(tmp_path)
    first = writer.write("t", _rows(1, 2))
    ref = writer.write("t", _rows(2), replace=True)
    iceberg = writer.catalog.load_table("ns.t")
    assert ref.files[0].location != first.files[0].location
    assert [t.file.file_path for t in iceberg.scan().plan_files()] == [ref.files[0].location]
    assert iceberg.scan().to_arrow()["instance"].to_pylist() == [2]
    assert ref.table_uuid == first.table_uuid and ref.snapshot_id != first.snapshot_id
    assert file_path(first.files[0].location).exists()


def test_write_puts_the_file_in_the_table_location_under_a_content_name(tmp_path):
    writer = _writer(tmp_path, partition_by="grid_tile")
    ref = writer.write("t", _rows(1, 2))
    location = writer.catalog.load_table("ns.t").location()
    [written] = ref.files
    assert re.fullmatch(rf"{re.escape(location)}/data/t-[0-9a-f]{{32}}\.parquet", written.location)
    assert pq.read_table(file_path(written.location)).num_rows == 2


def test_write_in_a_directory_with_a_space_reaches_the_table(tmp_path):
    writer = _writer(tmp_path / "a b")
    ref = writer.write("t", _rows(1))
    assert "a b" in str(file_path(ref.files[0].location))
    assert writer.catalog.load_table("ns.t").scan().to_arrow().num_rows == 1


def test_write_again_reuses_the_snapshot(tmp_path):
    writer = _writer(tmp_path)
    first = writer.write("t", _rows(1, 2))
    again = writer.write("t", _rows(1, 2))
    assert again == first
    assert writer.catalog.load_table("ns.t").scan().to_arrow().num_rows == 2


def test_write_with_other_rows_adds_an_object_and_leaves_the_first(tmp_path):
    writer = _writer(tmp_path)
    first = writer.write("t", _rows(1, 2))
    held = file_path(first.files[0].location).read_bytes()
    second = writer.write("t", _rows(3), replace=True)
    assert second.files[0].location != first.files[0].location
    assert file_path(first.files[0].location).read_bytes() == held
    assert writer.catalog.load_table("ns.t").scan().to_arrow().num_rows == 1


def test_a_write_after_a_replace_reuses_the_earlier_object_unchanged(tmp_path):
    writer = _writer(tmp_path)
    first = writer.write("t", _rows(1, 2))
    path = file_path(first.files[0].location)
    held, written_at = path.read_bytes(), path.stat().st_mtime_ns
    writer.write("t", _rows(2), replace=True)
    # The current snapshot no longer holds the first file, but its name still states its bytes.
    again = writer.write("t", _rows(1, 2), replace=True)
    assert again.files[0].location == first.files[0].location
    assert path.read_bytes() == held and path.stat().st_mtime_ns == written_at


def test_a_leftover_part_file_does_not_break_the_next_write(tmp_path):
    writer = _writer(tmp_path)
    ref = writer.write("t", _rows(1, 2))
    final = file_path(ref.files[0].location)
    expected = final.read_bytes()
    final.unlink()
    # A crash between the write and the rename leaves only this file.
    part = final.with_name(final.name + ".part")
    part.write_bytes(expected[: len(expected) // 2])
    assert not final.exists()
    writer.write("t", _rows(1, 2), replace=True)
    assert final.read_bytes() == expected
    assert sorted(p.name for p in final.parent.iterdir()) == [final.name, part.name]


def test_a_data_file_is_never_visible_under_its_final_name_while_it_is_written(tmp_path):
    writer = _writer(tmp_path)
    ref = writer.write("t", _rows(1, 2))
    final = file_path(ref.files[0].location)
    assert sorted(p.name for p in final.parent.iterdir()) == [final.name]


def test_write_parts_registers_several_files_in_one_snapshot(tmp_path):
    writer = _writer(tmp_path)
    ids = index_by_name(writer.catalog.load_table("ns.t").schema())
    paths = []
    for i in range(3):
        paths.append(tmp_path / f"p{i}.parquet")
        write_parquet(_rows(i), str(paths[-1]), index_column="instance", field_ids=ids)
    ref = writer.write_parts("t", paths)
    iceberg = writer.catalog.load_table("ns.t")
    assert len(ref.files) == 3 and len({f.location for f in ref.files}) == 3
    assert len(iceberg.metadata.snapshots) == 1
    assert sorted(iceberg.scan().to_arrow()["instance"].to_pylist()) == [0, 1, 2]


def _tile_file(writer, tmp_path, tile, *instances):
    table = pa.table({"instance": pa.array(instances, pa.int64()), "grid_tile": [tile] * len(instances)},
                     schema=WRITER_SCHEMA)
    path = tmp_path / f"{tile}.parquet"
    write_parquet(table, str(path), index_column="instance", field_ids=writer.field_ids("t"))
    return path


def _tiled_writer(tmp_path):
    return _writer(tmp_path / "catalog", partition_by="grid_tile")


def test_write_parts_without_replace_adds_to_the_table_and_returns_only_its_files(tmp_path):
    writer = _tiled_writer(tmp_path)
    first = writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0, 1)])
    second = writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 2)])
    assert [f.location for f in second.files] != [f.location for f in first.files]
    assert len(second.files) == 1
    table = writer.catalog.load_table("ns.t")
    assert sorted(table.scan().to_arrow()["instance"].to_pylist()) == [0, 1, 2]
    assert len(table.metadata.snapshots) == 2
    assert second.snapshot_id == table.current_snapshot().snapshot_id


def test_partitions_counts_the_records_of_each_partition_value(tmp_path):
    writer = _tiled_writer(tmp_path)
    assert writer.partitions("t") == {}
    writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0, 1)])
    writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 2)])
    assert writer.partitions("t") == {"a": 2, "b": 1}


def test_partitions_refuses_a_table_that_is_not_partitioned_by_one_column(tmp_path):
    with pytest.raises(ValueError, match="partition"):
        _writer(tmp_path / "catalog").partitions("t")


def test_ref_holds_every_file_of_the_current_snapshot_in_the_order_given(tmp_path):
    writer = _tiled_writer(tmp_path)
    a = writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0, 1)]).files[0]
    b = writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 2)]).files[0]
    ref = writer.ref("t", partition_order=["b", "a"])
    assert ref.files == [b, a]
    assert ref.snapshot_id == writer.catalog.load_table("ns.t").current_snapshot().snapshot_id
    assert ref.table_uuid == str(writer.catalog.load_table("ns.t").metadata.table_uuid)
    assert writer.ref("t", partition_order=["a", "b"]).files == [a, b]


def test_partition_ranges_give_the_min_and_max_of_a_column_in_each_partition(tmp_path):
    writer = _tiled_writer(tmp_path)
    writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 4, 1, 2)])
    writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 7)])
    assert writer.partition_ranges("t", "instance") == {"a": (1, 4), "b": (7, 7)}


def test_partition_ranges_refuse_a_file_without_bounds(tmp_path):
    writer = _tiled_writer(tmp_path)
    table = pa.table({"instance": pa.array([1, 2], pa.int64()), "grid_tile": ["a", "a"]}, schema=WRITER_SCHEMA)
    ids = writer.field_ids("t")
    schema = pa.schema([f.with_metadata({b"PARQUET:field_id": str(ids[f.name]).encode()}) for f in WRITER_SCHEMA])
    path = tmp_path / "a.parquet"
    pq.write_table(table.cast(schema), path, write_statistics=["grid_tile"])
    writer.write_parts("t", [path])
    with pytest.raises(ValueError, match="instance"):
        writer.partition_ranges("t", "instance")


def test_partitions_after_a_replace_leave_out_the_replaced_files(tmp_path):
    writer = _tiled_writer(tmp_path)
    writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0, 1)])
    writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 2)], replace=True)
    assert writer.partitions("t") == {"b": 1}
    assert writer.partition_ranges("t", "instance") == {"b": (2, 2)}


def test_ref_refuses_a_partition_value_outside_the_order(tmp_path):
    writer = _tiled_writer(tmp_path)
    writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0)])
    with pytest.raises(ValueError, match="a"):
        writer.ref("t", partition_order=["b"])


def test_ref_refuses_a_table_with_no_snapshot(tmp_path):
    with pytest.raises(ValueError, match="no snapshot"):
        _tiled_writer(tmp_path).ref("t")


def test_write_parts_with_replace_swaps_every_file_in_one_snapshot(tmp_path):
    writer = _writer(tmp_path)
    ids = index_by_name(writer.catalog.load_table("ns.t").schema())
    paths = [tmp_path / f"p{i}.parquet" for i in range(2)]
    for i, path in enumerate(paths):
        write_parquet(_rows(i + 10), str(path), index_column="instance", field_ids=ids)
    writer.write("t", _rows(1))
    writer.write_parts("t", paths, replace=True)
    iceberg = writer.catalog.load_table("ns.t")
    assert sorted(iceberg.scan().to_arrow()["instance"].to_pylist()) == [10, 11]
    assert len(iceberg.metadata.snapshots) == 2


def test_write_takes_a_parquet_file_as_it_is(tmp_path):
    writer = _writer(tmp_path)
    ids = index_by_name(writer.catalog.load_table("ns.t").schema())
    path = tmp_path / "p.parquet"
    write_parquet(_rows(5), str(path), index_column="instance", field_ids=ids)
    ref = writer.write("t", path)
    assert file_path(ref.files[0].location).read_bytes() == path.read_bytes()
    with pytest.raises(ValueError, match="geo"):
        writer.write("t", path, geo=GEO)


def test_write_refuses_a_table_that_was_not_required(tmp_path):
    writer = _writer(tmp_path)
    with pytest.raises(ValueError, match="other"):
        writer.write("other", _rows(1))
    with pytest.raises(ValueError, match="other"):
        writer.data_prefix("other")


def test_a_new_partitioned_table_is_created_in_one_commit(tmp_path):
    writer = _writer(tmp_path, partition_by="grid_tile")
    iceberg = writer.catalog.load_table("ns.t")
    assert [f.name for f in iceberg.spec().fields] == ["grid_tile"]
    assert len(iceberg.metadata.metadata_log) == 0


def test_require_sets_properties_on_an_existing_table(tmp_path):
    writer = _writer(tmp_path)
    writer.require("t", **(WRITER_SPEC | {"properties": {"p": "w", "q": "x"}}))
    assert writer.catalog.load_table("ns.t").properties["p"] == "w"
    assert writer.catalog.load_table("ns.t").properties["q"] == "x"


def test_data_prefix_covers_the_files_the_table_gets(tmp_path):
    writer = _writer(tmp_path)
    prefix = writer.data_prefix("t")
    ref = writer.write("t", _rows(1, 2))
    assert prefix.endswith("/data/") and ref.files[0].location.startswith(prefix)


def test_a_repo_with_the_data_prefixes_reads_the_copy(tmp_path):
    writer = _writer(tmp_path / "catalog")
    repo = create_repo(tmp_path / "repo", [], prefixes=[writer.data_prefix("t")])
    ref = writer.write("t", _rows(1, 2))
    root = zarr.open_group(repo.writable_session("main").store, mode="w")
    [array] = declare_copy(root, "t.parquet", ref.files)
    assert array[:].tobytes() == file_path(ref.files[0].location).read_bytes()


def test_a_rerun_reuses_a_written_table_and_commits_the_element(tmp_path):
    from minus80.core.elements import ElementWriter

    writer = _writer(tmp_path / "catalog")
    repo = create_repo(tmp_path / "repo", [], prefixes=[writer.data_prefix("t")])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    elements = ElementWriter(repo, "fixture")

    with pytest.raises(RuntimeError, match="commit failed"):
        with elements.unit("points/t", "Write points/t"):
            first = writer.write("t", _rows(1, 2))
            raise RuntimeError("commit failed")
    assert not elements.has("points/t")
    path = file_path(first.files[0].location)
    written_at = path.stat().st_mtime_ns

    with elements.unit("points/t", "Write points/t") as (root, _):
        again = writer.write("t", _rows(1, 2))
        group = root.require_group("points").create_group("t")
        group.attrs["iceberg_table"] = iceberg_table_attr(again, catalog="local", org="local", namespace="ns",
                                                          table="t", index_column="instance")
        declare_copy(group, "points.parquet", again.files)
    assert elements.has("points/t")
    stored = zarr.open_group(repo.readonly_session(branch="fixture").store, mode="r")["points/t"]
    assert stored["points.parquet"][:].tobytes() == path.read_bytes()
    assert again == first and path.stat().st_mtime_ns == written_at
    assert stored.attrs["iceberg_table"]["snapshot_id"] == first.snapshot_id
    assert writer.catalog.load_table("ns.t").scan().to_arrow().num_rows == 2


def test_write_to_a_remote_location_writes_through_the_table_file_io(tmp_path):
    writer, bucket = _remote_writer(tmp_path)
    ref = writer.write("t", _rows(1, 2))
    [written] = ref.files
    assert written.location.startswith("s3://bucket/wh/")
    stored = bucket / written.location.removeprefix("s3://bucket/")
    assert pq.read_table(stored).num_rows == 2 and written.size == stored.stat().st_size
    assert writer.catalog.load_table("ns.t").scan().to_arrow().num_rows == 2


def _remote_object(writer, bucket, rows):
    """The path in ``bucket`` where ``write`` of ``rows`` puts its object, and the file's bytes."""
    ref = writer.write("t", rows)
    path = bucket / ref.files[0].location.removeprefix("s3://bucket/")
    return ref, path, path.read_bytes()


def test_a_partial_remote_object_is_not_reused(tmp_path):
    writer, bucket = _remote_writer(tmp_path)
    ref, path, whole = _remote_object(writer, bucket, _rows(1, 2))
    path.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(ValueError, match="size") as error:
        writer.write("t", _rows(1, 2), replace=True)
    assert ref.files[0].location in str(error.value)
    assert path.read_bytes() == whole[: len(whole) // 2]


def test_a_complete_remote_object_is_reused_without_a_rewrite(tmp_path):
    writer, bucket = _remote_writer(tmp_path)
    ref, path, whole = _remote_object(writer, bucket, _rows(1, 2))
    written_at = path.stat().st_mtime_ns
    again = writer.write("t", _rows(1, 2))
    assert again == ref
    assert path.read_bytes() == whole and path.stat().st_mtime_ns == written_at


def test_a_remote_object_that_storage_cut_short_is_refused_and_not_registered(tmp_path):
    writer, bucket = _remote_writer(tmp_path, ShortBucketFileIO)
    with pytest.raises(ValueError, match="size") as error:
        writer.write("t", _rows(1, 2))
    [stored] = (bucket / "wh/ns/t/data").iterdir()
    assert f"s3://bucket/wh/ns/t/data/{stored.name}" in str(error.value)
    iceberg = writer.catalog.load_table("ns.t")
    assert iceberg.current_snapshot() is None
    assert list(iceberg.scan().plan_files()) == []


def _ready_parquet(writer, tmp_path, *instances):
    """A Parquet file in the form ``write`` produces, and its name under the table's data directory."""
    ids = index_by_name(writer.catalog.load_table("ns.t").schema())
    path = tmp_path / "ready.parquet"
    write_parquet(_rows(*instances), str(path), index_column="instance", field_ids=ids)
    return path


def test_a_local_object_at_the_final_name_is_reused_and_not_changed(tmp_path):
    writer = _writer(tmp_path)
    ready = _ready_parquet(writer, tmp_path, 1, 2)
    final = file_path(writer.write("t", ready).files[0].location)
    held, written_at = final.read_bytes(), final.stat().st_mtime_ns
    writer.write("t", ready, replace=True)
    assert final.read_bytes() == held and final.stat().st_mtime_ns == written_at


def test_a_remote_object_at_the_final_name_with_the_right_size_is_reused_and_not_changed(tmp_path):
    writer, bucket = _remote_writer(tmp_path)
    ready = _ready_parquet(writer, tmp_path, 1, 2)
    ref = writer.write("t", ready)
    path = bucket / ref.files[0].location.removeprefix("s3://bucket/")
    held, written_at = path.read_bytes(), path.stat().st_mtime_ns
    writer.write("t", ready, replace=True)
    assert path.read_bytes() == held and path.stat().st_mtime_ns == written_at


def test_a_remote_object_of_the_same_size_with_other_bytes_is_reused(tmp_path):
    writer, bucket = _remote_writer(tmp_path)
    ready = _ready_parquet(writer, tmp_path, 1, 2)
    ref = writer.write("t", ready)
    path = bucket / ref.files[0].location.removeprefix("s3://bucket/")
    altered = bytearray(path.read_bytes())
    altered[4] ^= 0xFF
    path.write_bytes(bytes(altered))
    writer.write("t", ready, replace=True)
    # The check reads only the size, so the altered object stays.
    assert path.read_bytes() == bytes(altered)


def test_a_failed_local_write_leaves_no_part_and_no_final_file(tmp_path):
    final = tmp_path / "data" / "f.parquet"
    with pytest.raises(FileNotFoundError):
        _write_local(tmp_path / "missing.parquet", final.as_uri())
    assert list(final.parent.iterdir()) == []


def test_a_local_write_leaves_no_temporary_file(tmp_path):
    source = tmp_path / "s.parquet"
    source.write_bytes(b"abc")
    final = tmp_path / "data" / "f.parquet"
    _write_local(source, final.as_uri())
    assert [p.name for p in final.parent.iterdir()] == ["f.parquet"] and final.read_bytes() == b"abc"


def test_an_existing_table_with_another_partition_spec_is_refused(tmp_path):
    writer = _writer(tmp_path, partition_by="grid_tile")
    with pytest.raises(ValueError, match=r"ns\.t.*grid_tile.*no partition|ns\.t.*grid_tile") as error:
        writer.require("t", **(WRITER_SPEC | {"partition_by": None}))
    assert "[]" in str(error.value)
    unpartitioned = _writer(tmp_path / "u")
    with pytest.raises(ValueError, match=r"ns\.t.*grid_tile"):
        unpartitioned.require("t", **(WRITER_SPEC | {"partition_by": "grid_tile"}))
    assert unpartitioned.catalog.load_table("ns.t").spec().fields == ()


def test_write_uses_the_index_column_of_the_writer(tmp_path):
    schema = pa.schema([pa.field("row", pa.int64(), nullable=False), ("v", pa.string())])
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local", index_column="row")
    writer.require("t", schema=schema, properties={})
    ref = writer.write("t", pa.table({"row": [1], "v": ["a"]}, schema=schema))
    pandas = json.loads(pq.read_schema(file_path(ref.files[0].location)).metadata[b"pandas"])
    assert pandas["index_columns"] == ["row"]


def test_write_takes_the_index_column_of_the_call_over_the_writers(tmp_path):
    schema = pa.schema([pa.field("row", pa.int64(), nullable=False), ("v", pa.string())])
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    writer.require("t", schema=schema, properties={})
    ref = writer.write("t", pa.table({"row": [1], "v": ["a"]}, schema=schema), index_column="row")
    pandas = json.loads(pq.read_schema(file_path(ref.files[0].location)).metadata[b"pandas"])
    assert pandas["index_columns"] == ["row"]


def test_partition_functions_read_the_snapshot_they_are_given_not_the_current_one(tmp_path):
    writer = _tiled_writer(tmp_path)
    first = writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0, 1)])
    writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 2)])
    table = writer.catalog.load_table("ns.t")
    held = partition_files(table, first.snapshot_id)
    assert partition_counts(held) == {"a": 2}
    assert partition_bounds(table, held, "instance") == {"a": (0, 1)}
    assert partition_counts(partition_files(table)) == {"a": 2, "b": 1}


def test_partition_files_refuse_a_table_that_is_not_partitioned_by_one_column(tmp_path):
    table = _writer(tmp_path / "catalog").catalog.load_table("ns.t")
    with pytest.raises(ValueError, match="ns.t.*partition"):
        partition_files(table)


def test_copy_edges_are_whole_chunks_then_the_remainder():
    assert copy_edges(25, 10) == [10, 10, 5]
    assert copy_edges(20, 10) == [10, 10]
    assert copy_edges(0, 10) == []
    assert copy_edges(3 * COPY_CHUNK + 1) == [COPY_CHUNK] * 3 + [1]


def test_row_group_bounds_give_the_min_max_and_rows_of_each_row_group_from_the_footer(tmp_path):
    from minus80.core.tables import row_group_bounds

    path = tmp_path / "g.parquet"
    pq.write_table(pa.table({"instance": pa.array([4, 5, 9, 1, 2], pa.int64())}), path, row_group_size=2)
    with open(path, "rb") as stream:
        assert row_group_bounds(stream, "instance") == [(4, 5, 2), (1, 9, 2), (2, 2, 1)]
    with open(path, "rb") as stream, pytest.raises(KeyError):
        row_group_bounds(stream, "other")


def test_row_group_bounds_refuse_a_file_without_statistics(tmp_path):
    from minus80.core.tables import row_group_bounds

    path = tmp_path / "g.parquet"
    pq.write_table(pa.table({"instance": pa.array([1, 2], pa.int64())}), path, write_statistics=False)
    with open(path, "rb") as stream, pytest.raises(ValueError, match="row group 0.*instance"):
        row_group_bounds(stream, "instance")


def test_partition_bounds_name_the_file_without_its_directory(tmp_path):
    writer = _tiled_writer(tmp_path)
    table = pa.table({"instance": pa.array([1, 2], pa.int64()), "grid_tile": ["a", "a"]}, schema=WRITER_SCHEMA)
    ids = writer.field_ids("t")
    schema = pa.schema([f.with_metadata({b"PARQUET:field_id": str(ids[f.name]).encode()}) for f in WRITER_SCHEMA])
    path = tmp_path / "a.parquet"
    pq.write_table(table.cast(schema), path, write_statistics=["grid_tile"])
    writer.write_parts("t", [path])
    with pytest.raises(ValueError) as raised:
        writer.partition_ranges("t", "instance")
    assert "/" not in str(raised.value) and ".parquet has no bounds for column instance" in str(raised.value)


EVOLVE_FROM = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string()), ("old", pa.int64())])
EVOLVE_TO = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string()), ("new", pa.int32())])


def _evolvable(tmp_path):
    """A writer whose table ``t`` has a column ``old`` that ``EVOLVE_TO`` lacks, with two rows written."""
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    writer.require("t", schema=EVOLVE_FROM, properties={})
    first = writer.write("t", pa.table({"instance": [1, 2], "grid_tile": ["a", "a"], "old": [5, 6]}, schema=EVOLVE_FROM))
    return writer, first


def test_require_refuses_a_column_difference_unless_it_evolves(tmp_path):
    writer, _ = _evolvable(tmp_path)
    with pytest.raises(ValueError, match=r"ns\.t.*adds new.*deletes old"):
        writer.require("t", schema=EVOLVE_TO, properties={})
    assert [f.name for f in writer.catalog.load_table("ns.t").schema().fields] == ["instance", "grid_tile", "old"]


def test_require_with_evolve_adds_and_deletes_columns_in_one_schema_commit(tmp_path):
    writer, _ = _evolvable(tmp_path)
    before = writer.catalog.load_table("ns.t")
    table = writer.require("t", schema=EVOLVE_TO, properties={}, evolve=True)
    assert [f.name for f in table.schema().fields] == ["instance", "grid_tile", "new"]
    assert len(table.metadata.schemas) == len(before.metadata.schemas) + 1
    assert table.schema().find_field("new").field_id > max(f.field_id for f in before.schema().fields)
    assert table.schema().find_field("instance").field_id == before.schema().find_field("instance").field_id
    assert table.schema().find_field("instance").required
    assert len(table.metadata.snapshots) == 1
    # A second evolve finds nothing to change.
    again = writer.require("t", schema=EVOLVE_TO, properties={}, evolve=True)
    assert len(again.metadata.schemas) == len(table.metadata.schemas)


def test_a_kept_column_of_another_type_is_refused_with_both_types(tmp_path):
    writer, _ = _evolvable(tmp_path)
    other = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string()), ("old", pa.string())])
    for evolve in (False, True):
        with pytest.raises(ValueError, match=r"column old is long.*has string"):
            writer.require("t", schema=other, properties={}, evolve=evolve)
    assert [f.name for f in writer.catalog.load_table("ns.t").schema().fields] == ["instance", "grid_tile", "old"]


def test_a_missing_required_column_is_refused_even_when_evolving(tmp_path):
    writer, _ = _evolvable(tmp_path)
    strict = pa.schema([pa.field("instance", pa.int64(), nullable=False), pa.field("new", pa.int32(), nullable=False)])
    with pytest.raises(ValueError, match=r"ns\.t has no column new"):
        writer.require("t", schema=strict, properties={}, evolve=True)


def test_a_struct_column_of_the_same_type_is_kept_when_evolving(tmp_path):
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    writer.require("t", schema=_NESTED, properties={})
    wider = pa.schema([*_NESTED, ("extra", pa.string())])
    table = writer.require("t", schema=wider, properties={}, evolve=True)
    assert [f.name for f in table.schema().fields] == ["instance", "bbox", "extra"]


def test_a_write_after_evolving_carries_the_evolved_field_ids_and_the_earlier_snapshot_keeps_its_columns(tmp_path):
    writer, first = _evolvable(tmp_path)
    writer.require("t", schema=EVOLVE_TO, properties={}, evolve=True)
    ref = writer.write("t", pa.table({"instance": [3], "grid_tile": ["a"], "new": pa.array([7], pa.int32())}, schema=EVOLVE_TO),
                       replace=True)
    table = writer.catalog.load_table("ns.t")
    footer = pq.read_schema(file_path(ref.files[0].location))
    assert {f.name: int(f.metadata[b"PARQUET:field_id"]) for f in footer} == index_by_name(table.schema())
    # pyiceberg scans a snapshot with the schema of that snapshot's write.
    earlier = table.scan(snapshot_id=first.snapshot_id).to_arrow()
    assert earlier.column_names == ["instance", "grid_tile", "old"]
    assert earlier["old"].to_pylist() == [5, 6]
    assert table.metadata.snapshot_by_id(first.snapshot_id).schema_id != table.metadata.current_schema_id
    assert file_path(first.files[0].location).exists()
    assert table.scan().to_arrow().column_names == ["instance", "grid_tile", "new"]


def test_partition_bounds_read_the_schema_of_the_snapshot_they_are_given(tmp_path):
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    writer.require("t", schema=EVOLVE_FROM, properties={}, partition_by="grid_tile")
    first = writer.write_parts("t", [_tile_file_of(writer, tmp_path, EVOLVE_FROM, {"old": [5, 9]})])
    writer.require("t", schema=EVOLVE_TO, properties={}, partition_by="grid_tile", evolve=True)
    table = writer.catalog.load_table("ns.t")
    held = partition_files(table, first.snapshot_id)
    assert partition_bounds(table, held, "old", snapshot_id=first.snapshot_id) == {"a": (5, 9)}


def _tile_file_of(writer, tmp_path, schema, columns):
    n = len(next(iter(columns.values())))
    table = pa.table({"instance": pa.array(range(n), pa.int64()), "grid_tile": ["a"] * n, **columns}, schema=schema)
    path = tmp_path / "tile.parquet"
    write_parquet(table, str(path), index_column="instance", field_ids=writer.field_ids("t"))
    return path


def test_register_files_replaces_the_named_partitions_in_the_one_snapshot_that_adds_the_file(tmp_path):
    writer = _tiled_writer(tmp_path)
    a = writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0, 1)])
    b = writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 2)])
    ref = writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 5, 6, 7)], replace_partitions=["a"])
    table = writer.catalog.load_table("ns.t")
    assert writer.partitions("t") == {"a": 3, "b": 1}
    assert len(table.metadata.snapshots) == 3 and ref.snapshot_id == table.current_snapshot().snapshot_id
    assert b.files[0].location in {t.file.file_path for t in table.scan().plan_files()}
    assert file_path(a.files[0].location).exists()
    assert sorted(table.scan(snapshot_id=b.snapshot_id).to_arrow()["instance"].to_pylist()) == [0, 1, 2]


def test_remove_partitions_deletes_their_files_from_the_current_snapshot_only(tmp_path):
    writer = _tiled_writer(tmp_path)
    a = writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0, 1)])
    writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 2)])
    writer.remove_partitions("t", ["a"])
    table = writer.catalog.load_table("ns.t")
    assert writer.partitions("t") == {"b": 1}
    assert len(table.metadata.snapshots) == 3
    assert file_path(a.files[0].location).exists()
    assert sorted(table.scan(snapshot_id=a.snapshot_id).to_arrow()["instance"].to_pylist()) == [0, 1]


def test_remove_partitions_without_a_match_commits_nothing(tmp_path):
    writer = _tiled_writer(tmp_path)
    writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0)])
    writer.remove_partitions("t", ["z"])
    assert len(writer.catalog.load_table("ns.t").metadata.snapshots) == 1


def test_write_without_replace_refuses_a_table_that_holds_a_file_of_other_content(tmp_path):
    writer = _writer(tmp_path)
    first = writer.write("t", _rows(1, 2))
    with pytest.raises(HeldFilesError, match=r"^ns\.t holds 1 data file of other content$") as held:
        writer.write("t", _rows(3))
    assert held.value.table == "t"
    table = writer.catalog.load_table("ns.t")
    assert len(table.metadata.snapshots) == 1
    assert [t.file.file_path for t in table.scan().plan_files()] == [first.files[0].location]
    # The same content has the same name, so it is a no-op.
    assert writer.write("t", _rows(1, 2)).snapshot_id == first.snapshot_id


def test_write_parts_adds_a_file_to_a_table_that_holds_others(tmp_path):
    writer = _tiled_writer(tmp_path)
    writer.write_parts("t", [_tile_file(writer, tmp_path, "a", 0)])
    writer.write_parts("t", [_tile_file(writer, tmp_path, "b", 1)])
    assert writer.partitions("t") == {"a": 1, "b": 1}


def test_a_type_mismatch_prints_types_without_field_ids(tmp_path):
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    writer.require("t", schema=_NESTED, properties={})
    other = pa.schema([pa.field("instance", pa.int64(), nullable=False),
                       ("bbox", pa.struct([("xmin", pa.float64()), ("xmax", pa.string())]))])
    with pytest.raises(ValueError, match=r"column bbox is struct<xmin: double, xmax: double>, the wanted schema has") as e:
        writer.require("t", schema=other, properties={}, evolve=True)
    assert "-1" not in str(e.value) and not any(ch.isdigit() for ch in str(e.value).split("column bbox")[1])


def test_a_struct_child_that_differs_only_in_nullability_is_the_same_type(tmp_path):
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    writer.require("t", schema=_NESTED, properties={})
    strict = pa.schema([pa.field("instance", pa.int64(), nullable=False),
                        ("bbox", pa.struct([pa.field("xmin", pa.float64(), nullable=False), ("xmax", pa.float64())]))])
    assert writer.require("t", schema=strict, properties={}, evolve=True).schema().find_field("bbox")


def test_evolving_leaves_the_type_and_required_flag_of_a_kept_column(tmp_path):
    writer, _ = _evolvable(tmp_path)
    loose = pa.schema([("instance", pa.int64()), ("grid_tile", pa.string()), ("new", pa.int32())])
    table = writer.require("t", schema=loose, properties={}, evolve=True)
    assert str(table.schema().find_field("instance").field_type) == "long"
    assert table.schema().find_field("instance").required


def test_evolving_adds_a_struct_column_with_fresh_field_ids(tmp_path):
    writer, _ = _evolvable(tmp_path)
    wider = pa.schema([*EVOLVE_FROM, ("box", pa.struct([("a", pa.float64()), ("b", pa.float64())]))])
    table = writer.require("t", schema=wider, properties={}, evolve=True)
    ids = list(index_by_name(table.schema()).values())
    assert len(ids) == len(set(ids)) and all(i > 0 for i in ids)


def test_a_dictionary_column_makes_pyiceberg_log_no_warning(tmp_path, caplog):
    schema = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("name", pa.dictionary(pa.int32(), pa.string()))])
    writer = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    with caplog.at_level("WARNING", logger="pyiceberg"):
        writer.require("t", schema=schema, properties={})
        table = writer.require("t", schema=schema, properties={}, evolve=True)
        writer.require("t", schema=pa.schema([*schema, ("extra", pa.dictionary(pa.int32(), pa.string()))]),
                       properties={}, evolve=True)
    assert [r.getMessage() for r in caplog.records] == []
    assert str(table.schema().find_field("name").field_type) == "string"


class _ExpiringOutput:
    """An output file whose upload fails on close and leaves no object, as an expired credential does."""

    def __init__(self, output, fails):
        self._output, self._fails = output, fails

    def __getattr__(self, attribute):
        return getattr(self._output, attribute)

    def __len__(self):
        return len(self._output)

    def create(self, overwrite=False):
        stream = self._output.create(overwrite=overwrite)
        if not self._fails:
            return stream
        output = self._output

        class Failing:
            def write(self, data):
                return stream.write(data)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                stream.close()
                output._filesystem.delete_file(output._path)
                raise OSError("AWS Error SIGNATURE_DOES_NOT_MATCH during UploadPart operation")

        return Failing()


class ExpiringBucketFileIO(BucketFileIO):
    """A bucket whose data-file uploads fail while ``failures`` stays above 0; each load of a table counts in ``loads``."""

    failures = 0
    loads = 0

    def __init__(self, properties=None):
        super().__init__(properties or {})
        type(self).loads += 1

    def new_output(self, location):
        fails = "/data/" in location and type(self).failures > 0
        type(self).failures -= fails
        return _ExpiringOutput(super().new_output(location), fails)


def test_an_upload_that_fails_loads_the_table_again_and_is_repeated(tmp_path, capsys):
    writer, bucket = _remote_writer(tmp_path, ExpiringBucketFileIO)
    ExpiringBucketFileIO.failures, ExpiringBucketFileIO.loads = 1, 0
    ref = writer.write_parts("t", [_ready_parquet(writer, tmp_path, 1, 2)])
    assert ExpiringBucketFileIO.failures == 0 and ExpiringBucketFileIO.loads >= 2
    assert len(list((bucket / "wh/ns/t/data").iterdir())) == 1
    assert [t.file.file_path for t in writer.catalog.load_table("ns.t").scan().plan_files()] == [ref.files[0].location]
    out = capsys.readouterr().out
    assert "t: an upload failed (OSError); attempt 2 of 3 loads the table again for new credentials" in out
    assert "s3://" not in out


def test_an_upload_that_fails_each_attempt_raises_and_registers_nothing(tmp_path):
    writer, bucket = _remote_writer(tmp_path, ExpiringBucketFileIO)
    ExpiringBucketFileIO.failures = UPLOAD_ATTEMPTS
    with pytest.raises(OSError, match="SIGNATURE_DOES_NOT_MATCH"):
        writer.write_parts("t", [_ready_parquet(writer, tmp_path, 1, 2)])
    assert ExpiringBucketFileIO.failures == 0
    assert list((bucket / "wh/ns/t/data").iterdir()) == []
    assert writer.catalog.load_table("ns.t").current_snapshot() is None
