import numpy as np
import pyarrow as pa
import pytest

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


# Three tiles in source order: 5 rows, none, 1 row.
TILES = {"0,0": 5, "1,0": 0, "-1,0": 1}


class _Bundle:
    def __init__(self, zf):
        self.zf = zf

    def member_zip(self, name):
        return self.zf


def _transcripts_zip(tmp_path):
    from .stores import zip_v2_store

    rng = np.random.default_rng(0)
    arrays = {"codeword_category": (np.zeros((2, 9), dtype=bool), {})}
    for tile, n in TILES.items():
        arrays |= {
            f"grid/{tile}/location": (rng.random((n, 3), dtype=np.float32), {"chunks": (2, 3)}),
            f"grid/{tile}/codeword_identity": (rng.integers(0, 2, (n, 1)).astype("<u4"), {"chunks": (2, 1)}),
            f"grid/{tile}/cell_id": (rng.integers(0, 2**32, (n, 2), dtype=np.uint32), {"chunks": (2, 2)}),
            f"grid/{tile}/quality_score": (rng.random((n, 1)).astype("<f2"), {"chunks": (2, 1)}),
            f"grid/{tile}/overlaps_nucleus": (rng.integers(0, 2, (n, 1)).astype("u1"), {"chunks": (2, 1)}),
        }
    return zip_v2_store(
        tmp_path, "transcripts", arrays,
        attrs={"codeword_gene_names": ["A", "B"], "codeword_gene_mapping": [0, 1]},
        group_attrs={"grid": {"grid_keys": list(TILES), "grid_number_objects": list(TILES.values())}},
    )


def _transcripts_run(tmp_path):
    """A zip, a table writer with its table, and an element writer on a fresh local repo."""
    from ingest import table_specs
    from minus80.core.elements import ElementWriter
    from minus80.core.icechunk_io import create_repo

    zf = _transcripts_zip(tmp_path)
    tables = TableWriter(local_catalog(tmp_path / "catalog"), "ns", catalog_name="local", org="local")
    tables.require("transcripts", **table_specs({}, [])["transcripts"])
    repo = create_repo(tmp_path / "repo", [], prefixes=[tables.data_prefix("transcripts")])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    return _Bundle(zf), tables, repo, ElementWriter(repo, "fixture")


def _copied_tiles(repo):
    """The grid tile of each Parquet part of the element's copy, in part order."""
    import io

    import pyarrow.parquet as pq
    import zarr

    group = zarr.open_group(repo.readonly_session(branch="fixture").store, mode="r")["points/transcripts"]
    with zarr.config.set({"array.rectilinear_chunks": True}):
        parts = sorted(group["points.parquet"].members(), key=lambda m: int(m[0].split(".")[1]))
        return [pq.read_table(io.BytesIO(a[:].tobytes()))["grid_tile"][0].as_py() for _, a in parts]


def test_every_tile_is_written_with_instances_counted_across_tiles(tmp_path):
    from ingest import write_transcripts

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    write_transcripts(writer, bundle, tables)
    table = tables.catalog.load_table("ns.transcripts").scan().to_arrow()
    by_instance = table.sort_by("instance")
    assert by_instance["instance"].to_pylist() == list(range(6))
    assert by_instance["grid_tile"].to_pylist() == ["0,0"] * 5 + ["-1,0"]
    assert tables.partitions("transcripts") == {"0,0": 5, "-1,0": 1}
    assert writer.has("points/transcripts")
    assert _copied_tiles(repo) == ["0,0", "-1,0"]


def test_a_run_that_stopped_resumes_without_writing_a_held_tile_again(tmp_path, monkeypatch, capsys):
    import ingest
    from minus80.core.registry import file_path

    stopped = tmp_path / "stopped"
    stopped.mkdir()
    bundle, tables, repo, writer = _transcripts_run(stopped)
    real, calls = ingest.write_tile, []

    def stops_at_the_second_tile(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == 2:
            raise RuntimeError("stop")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "write_tile", stops_at_the_second_tile)
    with pytest.raises(RuntimeError, match="stop"):
        ingest.write_transcripts(writer, bundle, tables)
    assert not writer.has("points/transcripts")
    [held] = tables.ref("transcripts").files
    written_at = file_path(held.location).stat().st_mtime_ns

    monkeypatch.setattr(ingest, "write_tile", lambda *a, **k: calls.append(a[1]) or real(*a, **k))
    calls.clear()
    capsys.readouterr()
    ingest.write_transcripts(writer, bundle, tables)
    assert calls == ["-1,0"]
    assert "tile 0,0: in the table already, skipped" in capsys.readouterr().out
    assert file_path(held.location).stat().st_mtime_ns == written_at
    assert len(tables.catalog.load_table("ns.transcripts").metadata.snapshots) == 2

    whole = tmp_path / "whole"
    whole.mkdir()
    bundle2, tables2, repo2, writer2 = _transcripts_run(whole)
    ingest.write_transcripts(writer2, bundle2, tables2)
    rows = lambda t: t.catalog.load_table("ns.transcripts").scan().to_arrow().sort_by("instance")  # noqa: E731
    assert rows(tables).equals(rows(tables2))
    assert _copied_tiles(repo) == _copied_tiles(repo2) == ["0,0", "-1,0"]


def test_a_held_tile_with_another_row_count_is_refused(tmp_path):
    from ingest import write_transcripts
    from minus80.core.tables import write_parquet
    from transcripts import codeword_table, tile_table

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    short = tmp_path / "short.parquet"
    write_parquet(tile_table(bundle.zf, "0,0", codeword_table(bundle.zf), first_instance=0).slice(0, 2),
                  str(short), index_column="instance", field_ids=tables.field_ids("transcripts"))
    tables.write_parts("transcripts", [short])
    with pytest.raises(ValueError, match=r"tile 0,0: the table holds 2 rows, the source has 5"):
        write_transcripts(writer, bundle, tables)
    assert len(tables.catalog.load_table("ns.transcripts").metadata.snapshots) == 1


def test_a_given_tile_list_counts_instances_from_zero_and_leaves_no_scratch_file(tmp_path):
    from ingest import write_transcripts

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    write_transcripts(writer, bundle, tables, tiles=["-1,0"], scratch=scratch)
    table = tables.catalog.load_table("ns.transcripts").scan().to_arrow()
    assert table["instance"].to_pylist() == [0] and table["grid_tile"].to_pylist() == ["-1,0"]
    assert list(scratch.iterdir()) == []
