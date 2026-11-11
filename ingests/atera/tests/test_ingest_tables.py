import pyarrow as pa
import pyarrow.parquet as pq

from ingest import Tables, local_catalog, write_local
from minus80.core.registry import file_path

SCHEMA = pa.schema([("instance", pa.int64()), ("grid_tile", pa.string())])


def _tables(directory):
    return Tables(local_catalog(directory), "ns", kind="local", org="local", write_file=write_local)


def _table():
    return pa.table({"instance": pa.array([1, 2], pa.int64()), "grid_tile": ["0,0", "0,0"]})


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
