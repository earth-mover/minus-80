import pyarrow as pa
import pyarrow.parquet as pq

from ingest import Tables, local_catalog, write_local


def test_tables_writes_the_file_in_the_table_location_and_pins_the_snapshot(tmp_path):
    catalog = local_catalog(tmp_path)
    tables = Tables(catalog, "ns", kind="local", org="local", write_file=write_local)
    table = pa.table({"instance": pa.array([1, 2], pa.int64()), "grid_tile": ["0,0", "0,0"]})
    ref, attr = tables.write("t", table, schema=table.schema, properties={"p": "v"}, partition_by="grid_tile")
    location = catalog.load_table("ns.t").location()
    assert [f for f, _ in ref.files] == [f"{location}/data/t-0.parquet"]
    assert pq.read_table(ref.files[0][0].removeprefix("file://")).num_rows == 2
    assert attr["catalog"] == "local" and attr["org"] == "local" and attr["table"] == "t"
    assert attr["snapshot_id"] == catalog.load_table("ns.t").current_snapshot().snapshot_id
    assert "geometry_column" not in attr
