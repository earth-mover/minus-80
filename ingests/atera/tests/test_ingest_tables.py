import pyarrow as pa

from ingest import table_attr
from minus80.core.tables import TableWriter, local_catalog

SCHEMA = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string())])


def test_table_attr_pins_the_snapshot_the_writer_registered(tmp_path):
    tables = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    tables.require("t", schema=SCHEMA, properties={})
    ref = tables.write("t", pa.table({"instance": [1], "grid_tile": ["0,0"]}, schema=SCHEMA))
    attr = table_attr(tables, "t", ref)
    assert (attr["catalog"], attr["org"], attr["namespace"], attr["table"]) == ("local", "local", "ns", "t")
    assert attr["snapshot_id"] == tables.catalog.load_table("ns.t").current_snapshot().snapshot_id
    assert "geometry_column" not in attr
    assert table_attr(tables, "t", ref, geo={"x": 1})["geometry_column"] == "geometry"


def test_table_specs_partition_the_transcripts_and_describe_the_polygons():
    from ingest import table_specs

    specs = table_specs({"license": "x"}, ["m"])
    assert specs["transcripts"]["partition_by"] == "grid_tile"
    assert specs["nucleus_boundaries"]["properties"]["method_names"] == '["m"]'
    assert "geo" in specs["nucleus_boundaries"]["properties"]


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


def test_rewrite_refuses_the_main_branch(monkeypatch, tmp_path):
    import sys
    import pytest
    import ingest

    def no_bundle(location):
        raise AssertionError("main() opened the bundle")

    # A regression must not read 10x's bucket or write into the working directory.
    monkeypatch.setattr(ingest, "open_bundle", no_bundle)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ingest.py", "--sample", "kidney", "--repo", "r", "--catalog", "c",
                                      "--namespace", "n", "--branch", "main", "--rewrite", "points/transcripts"])
    with pytest.raises(SystemExit, match="--rewrite never writes to main"):
        ingest.main()
