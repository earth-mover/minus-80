import pyarrow as pa
import pyarrow.parquet as pq

from ingest import Tables, local_catalog, write_local
from minus80.core.registry import file_path

SCHEMA = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string())])


def _tables(directory):
    return Tables(local_catalog(directory), "ns", kind="local", org="local", write_file=write_local)


def _table():
    return pa.table({"instance": pa.array([1, 2], pa.int64()), "grid_tile": ["0,0", "0,0"]}, schema=SCHEMA)


def test_tables_write_gives_the_file_the_table_field_ids_and_a_required_instance(tmp_path):
    from pyiceberg.schema import index_by_name

    tables = _tables(tmp_path)
    ref, _ = tables.write("t", _table(), schema=SCHEMA, properties={})
    iceberg = tables.catalog.load_table("ns.t")
    footer = pq.read_schema(file_path(ref.files[0][0]))
    assert {f.name: int(f.metadata[b"PARQUET:field_id"]) for f in footer} == index_by_name(iceberg.schema())
    assert not footer.field("instance").nullable
    assert iceberg.schema().find_field("instance").required


def test_tables_write_with_replace_swaps_the_file_and_keeps_the_table(tmp_path):
    tables = _tables(tmp_path)
    first, _ = tables.write("t", _table(), schema=SCHEMA, properties={})
    ref, attr = tables.write("t", _table().slice(1), schema=SCHEMA, properties={}, replace=True)
    iceberg = tables.catalog.load_table("ns.t")
    assert [f for f, _ in ref.files] == [f"{iceberg.location()}/data/t-1.parquet"]
    assert [t.file.file_path for t in iceberg.scan().plan_files()] == [ref.files[0][0]]
    assert iceberg.scan().to_arrow()["instance"].to_pylist() == [2]
    assert ref.table_uuid == first.table_uuid and attr["snapshot_id"] == ref.snapshot_id != first.snapshot_id
    assert file_path(first.files[0][0]).exists()


def test_tables_writes_the_file_in_the_table_location_and_pins_the_snapshot(tmp_path):
    tables = _tables(tmp_path)
    ref, attr = tables.write("t", _table(), schema=SCHEMA, properties={"p": "v"}, partition_by="grid_tile")
    location = tables.catalog.load_table("ns.t").location()
    assert [f for f, _ in ref.files] == [f"{location}/data/t-0.parquet"]
    assert pq.read_table(file_path(ref.files[0][0])).num_rows == 2
    assert attr["catalog"] == "local" and attr["org"] == "local" and attr["table"] == "t"
    assert attr["snapshot_id"] == tables.catalog.load_table("ns.t").current_snapshot().snapshot_id
    assert "geometry_column" not in attr


def test_tables_write_again_reuses_the_snapshot(tmp_path):
    tables = _tables(tmp_path)
    first, _ = tables.write("t", _table(), schema=SCHEMA, properties={})
    again, _ = tables.write("t", _table(), schema=SCHEMA, properties={})
    assert again == first
    assert tables.catalog.load_table("ns.t").scan().to_arrow().num_rows == 2


def test_data_prefix_covers_the_files_the_table_gets(tmp_path):
    tables = _tables(tmp_path)
    prefix = tables.data_prefix("t", schema=SCHEMA, properties={})
    ref, _ = tables.write("t", _table(), schema=SCHEMA, properties={})
    assert prefix.endswith("/data/") and ref.files[0][0].startswith(prefix)


def test_write_local_decodes_the_url(tmp_path):
    directory = tmp_path / "a b"
    write_local(None, (directory / "f.parquet").as_uri(), _table(), geo=None)
    assert (directory / "f.parquet").exists()


def test_a_repo_with_the_data_prefixes_reads_the_copy(tmp_path):
    import zarr
    from minus80.core.icechunk_io import create_repo
    from minus80.core.tables import declare_copy

    tables = _tables(tmp_path / "catalog")
    prefix = tables.data_prefix("t", schema=SCHEMA, properties={})
    repo = create_repo(tmp_path / "repo", [], prefixes=[prefix])
    ref, _ = tables.write("t", _table(), schema=SCHEMA, properties={})
    root = zarr.open_group(repo.writable_session("main").store, mode="w")
    with zarr.config.set({"array.rectilinear_chunks": True}):
        [array] = declare_copy(root, "t.parquet", ref.files)
        assert array[:].tobytes() == file_path(ref.files[0][0]).read_bytes()


def test_table_specs_partition_the_transcripts_and_describe_the_polygons():
    from ingest import table_specs

    specs = table_specs({"license": "x"}, ["m"])
    assert specs["transcripts"]["partition_by"] == "grid_tile"
    assert specs["nucleus_boundaries"]["properties"]["method_names"] == '["m"]'
    assert "geo" in specs["nucleus_boundaries"]["properties"]


def test_writer_holds_only_the_elements_a_commit_wrote(tmp_path):
    from ingest import ReadCounter, Writer
    from minus80.core.icechunk_io import create_repo

    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    writer = Writer(repo, "fixture", ReadCounter(None))
    assert not writer.holds("labels/nucleus_labels")
    with writer.element("labels/nucleus_labels", None) as root:
        root.require_group("labels").create_group("nucleus_labels")
    assert writer.holds("labels/nucleus_labels")
    assert not writer.holds("labels/cell_labels")


def test_an_element_that_raises_is_not_committed(tmp_path):
    import pytest
    from ingest import ReadCounter, Writer
    from minus80.core.icechunk_io import create_repo

    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    writer = Writer(repo, "fixture", ReadCounter(None))
    snapshot = repo.lookup_branch("fixture")
    with pytest.raises(RuntimeError, match="read failed"):
        with writer.element("labels/nucleus_labels", None) as root:
            root.require_group("labels").create_group("nucleus_labels")
            raise RuntimeError("read failed")
    assert repo.lookup_branch("fixture") == snapshot
    assert not writer.holds("labels/nucleus_labels")


def test_a_rerun_reuses_a_written_table_and_commits_the_element(tmp_path):
    import pytest
    import zarr
    from ingest import ReadCounter, Writer
    from minus80.core.icechunk_io import create_repo
    from minus80.core.tables import declare_copy

    writes = []

    def counted_write(io, url, table, *, geo, field_ids):
        writes.append(url)
        write_local(io, url, table, geo=geo, field_ids=field_ids)

    tables = Tables(local_catalog(tmp_path / "catalog"), "ns", kind="local", org="local", write_file=counted_write)
    prefix = tables.data_prefix("t", schema=SCHEMA, properties={})
    repo = create_repo(tmp_path / "repo", [], prefixes=[prefix])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    writer = Writer(repo, "fixture", ReadCounter(None))

    with pytest.raises(RuntimeError, match="commit failed"):
        with writer.element("points/t", None):
            first, _ = tables.write("t", _table(), schema=SCHEMA, properties={})
            raise RuntimeError("commit failed")
    assert not writer.holds("points/t")

    with zarr.config.set({"array.rectilinear_chunks": True}):
        with writer.element("points/t", None) as root:
            again, attr = tables.write("t", _table(), schema=SCHEMA, properties={})
            group = root.require_group("points").create_group("t")
            group.attrs["iceberg_table"] = attr
            declare_copy(group, "points.parquet", again.files)
        assert writer.holds("points/t")
        stored = zarr.open_group(repo.readonly_session(branch="fixture").store, mode="r")["points/t"]
        assert stored["points.parquet"][:].tobytes() == file_path(again.files[0][0]).read_bytes()
    assert again == first
    assert len(writes) == 1
    assert stored.attrs["iceberg_table"]["snapshot_id"] == first.snapshot_id
    assert tables.catalog.load_table("ns.t").scan().to_arrow().num_rows == 2


def test_drop_index_column_leaves_the_index_array_and_reads_with_anndata(tmp_path):
    import pandas as pd
    import zarr
    from anndata.io import read_elem, write_elem
    from ingest import drop_index_column

    group = zarr.open_group(tmp_path / "t.zarr", mode="w")
    write_elem(group, "obs", pd.DataFrame({"n": [1, 2]}, index=pd.Index(["a", "b"], name="barcode")))
    obs = group["obs"]
    # The source lists the index in column-order too.
    obs.attrs["column-order"] = ["barcode", *obs.attrs["column-order"]]
    drop_index_column(obs)
    assert obs.attrs["column-order"] == ["n"]
    assert "barcode" in obs
    frame = read_elem(obs)
    assert list(frame.columns) == ["n"] and list(frame.index) == ["a", "b"]


def test_rewrite_elements_accepts_only_the_rewritable_elements():
    import pytest
    from ingest import rewrite_elements

    assert rewrite_elements("points/transcripts,tables/table:attrs") == {"points/transcripts", "tables/table:attrs"}
    with pytest.raises(ValueError, match="labels/cell_labels"):
        rewrite_elements("labels/cell_labels")
