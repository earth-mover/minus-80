import io
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
import zarr

from .stores import cells_zip
from ingest import table_attr
from minus80.core.tables import TableWriter, local_catalog

LAYOUTS = (Path(__file__).parents[1] / "LAYOUTS.md").read_text()
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
    for name in ("nucleus_boundaries", "cell_boundaries"):
        assert specs[name]["properties"]["method_names"] == '["m"]'
        assert "geo" in specs[name]["properties"]


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
    from tables_extra import codewords_table
    from transcripts import tile_table

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    short = tmp_path / "short.parquet"
    write_parquet(tile_table(bundle.zf, "0,0", codewords_table(bundle.zf), first_instance=0).slice(0, 2),
                  str(short), index_column="instance", field_ids=tables.field_ids("transcripts"))
    tables.write_parts("transcripts", [short])
    with pytest.raises(ValueError, match=r"tile 0,0: the table holds 2 rows.*expected 5 rows"):
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


def _hold(tables, bundle, tmp_path, tile, *, first_instance, name=None, rows=None):
    """Register a real tile file in the table, optionally under another partition value or cut to ``rows``."""
    from minus80.core.tables import write_parquet
    from tables_extra import codewords_table
    from transcripts import tile_table

    table = tile_table(bundle.zf, tile, codewords_table(bundle.zf), first_instance=first_instance)
    if rows is not None:
        table = table.slice(0, rows)
    if name is not None:
        table = table.set_column(1, table.schema.field("grid_tile"), pa.repeat(name, len(table)))
    path = tmp_path / "held.parquet"
    write_parquet(table, str(path), index_column="instance", field_ids=tables.field_ids("transcripts"))
    tables.write_parts("transcripts", [path])


def test_a_held_tile_written_from_another_origin_is_refused(tmp_path):
    from ingest import write_transcripts

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=10)
    # The manifest bounds of `instance` come from the Parquet statistics.
    assert tables.partition_ranges("transcripts", "instance") == {"0,0": (10, 14)}
    with pytest.raises(ValueError, match=r"tile 0,0: .*10\.\.14.*0\.\.4"):
        write_transcripts(writer, bundle, tables)
    assert len(tables.catalog.load_table("ns.transcripts").metadata.snapshots) == 1


def test_a_table_partition_outside_the_plan_is_refused_before_any_tile_is_written(tmp_path):
    from ingest import write_transcripts

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, name="9,9")
    with pytest.raises(ValueError, match="9,9"):
        write_transcripts(writer, bundle, tables)
    assert len(tables.catalog.load_table("ns.transcripts").metadata.snapshots) == 1


def test_a_failed_tile_leaves_no_scratch_file(tmp_path, monkeypatch):
    import ingest

    def writes_a_part_then_fails(zf, tile, codewords, path, **kwargs):
        path.write_bytes(b"part")
        raise RuntimeError("stop")

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(ingest, "write_tile", writes_a_part_then_fails)
    with pytest.raises(RuntimeError, match="stop"):
        ingest.write_transcripts(writer, bundle, tables, scratch=scratch)
    assert list(scratch.iterdir()) == []


def test_a_tile_that_the_source_lacks_is_refused(tmp_path):
    from ingest import write_transcripts

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    with pytest.raises(ValueError, match="7,7"):
        write_transcripts(writer, bundle, tables, tiles=["7,7"])


def test_the_rate_counts_written_rows_only(tmp_path, monkeypatch, capsys):
    import ingest
    from types import SimpleNamespace

    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0)
    ticks = iter([0.0] + [0.5] * 10)
    monkeypatch.setattr(ingest, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    ingest.write_transcripts(writer, bundle, tables)
    # One row written in 0.5 s; the five held rows are in the total only.
    assert "tile -1,0: 1 rows, 6 of 6, 2 rows/s" in capsys.readouterr().out


def _boundaries_run(tmp_path):
    from ingest import table_specs
    from minus80.core.elements import ElementWriter
    from minus80.core.icechunk_io import create_repo

    bundle = _Bundle(cells_zip(tmp_path))
    tables = TableWriter(local_catalog(tmp_path / "catalog"), "ns", catalog_name="local", org="local")
    specs = table_specs({}, ["a", "b", "c", "d", "e"])
    for name in ("nucleus_boundaries", "cell_boundaries"):
        tables.require(name, **specs[name])
    prefixes = [tables.data_prefix(n) for n in ("nucleus_boundaries", "cell_boundaries")]
    repo = create_repo(tmp_path / "repo", [], prefixes=prefixes)
    repo.create_branch("fixture", repo.lookup_branch("main"))
    return bundle, tables, repo, ElementWriter(repo, "fixture")


@pytest.mark.parametrize("name, set_index, instances, parts", [
    ("nucleus_boundaries", 0, [1, 3], [1, 2]),
    ("cell_boundaries", 1, [1, 2, 3], [1, 1, 1]),
])
def test_write_boundaries_writes_one_row_per_cell_of_the_set(tmp_path, name, set_index, instances, parts):
    from ingest import write_boundaries

    bundle, tables, repo, writer = _boundaries_run(tmp_path)
    write_boundaries(writer, bundle, tables, name, set_index)
    table = tables.catalog.load_table(f"ns.{name}").scan().to_arrow().sort_by("instance")
    assert table["instance"].to_pylist() == instances
    assert table["part_count"].to_pylist() == parts
    assert table["cell_id"].to_pylist() == [f"aaaaaaa{'bcd'[i - 1]}-1" for i in instances]
    assert writer.has(f"shapes/{name}")
    group = zarr.open_group(repo.readonly_session(branch="fixture").store, mode="r")[f"shapes/{name}"]
    attr = group.attrs["iceberg_table"]
    assert attr["table"] == name and attr["index_column"] == "instance" and attr["geometry_column"] == "geometry"
    assert attr["snapshot_id"] == tables.catalog.load_table(f"ns.{name}").current_snapshot().snapshot_id


class _ExtraBundle:
    """A transcripts zip and one differential expression CSV for both members."""

    def __init__(self, tmp_path):
        from .stores import zip_v2_store

        self.transcripts = zip_v2_store(
            tmp_path, "transcripts", {"codeword_category": (np.zeros((2, 9), dtype=bool), {})},
            attrs={"codeword_gene_names": ["A", "B"], "codeword_gene_mapping": [0, 1]},
        )

    def member_zip(self, name):
        return self.transcripts

    def member_file(self, name):
        return io.BytesIO(b"Feature ID,Feature Name,Amacrine cell Mean Counts\nENSG1,A,0.5\n")


def _extra_run(tmp_path):
    from minus80.core.elements import ElementWriter
    from minus80.core.icechunk_io import create_repo
    from sources import SAMPLES

    tables = TableWriter(local_catalog(tmp_path / "catalog"), "ns", catalog_name="local", org="local")
    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    return _ExtraBundle(tmp_path), tables, repo, ElementWriter(repo, "fixture"), SAMPLES["kidney"], LAYOUTS


def _pinned(repo):
    return zarr.open_group(repo.readonly_session(branch="fixture").store, mode="r")


def test_write_extra_tables_pins_each_table_in_one_root_attribute(tmp_path, capsys):
    from ingest import ICEBERG_TABLES, write_extra_tables

    bundle, tables, repo, writer, sample, layouts = _extra_run(tmp_path)
    write_extra_tables(writer, bundle, tables, sample, layouts, "r")
    root = _pinned(repo)
    pinned = root.attrs[ICEBERG_TABLES]
    assert list(pinned) == ["codewords", "diffexp_azimuth", "diffexp_graphclust"]
    assert pinned["codewords"]["index_column"] == "codeword_index"
    assert pinned["diffexp_azimuth"]["index_column"] == "Feature ID"
    assert all("geometry_column" not in entry for entry in pinned.values())
    for name, entry in pinned.items():
        table = tables.catalog.load_table(f"ns.{entry['table']}")
        assert entry["snapshot_id"] == table.current_snapshot().snapshot_id
        assert entry["table_uuid"] == str(table.metadata.table_uuid)
    assert not any(group for group, _ in root.groups())
    capsys.readouterr()
    write_extra_tables(writer, bundle, tables, sample, layouts, "r")
    assert "skipped" in capsys.readouterr().out
    assert len(tables.catalog.load_table("ns.codewords").metadata.snapshots) == 1


def test_the_extra_tables_say_that_the_root_attribute_pins_them(tmp_path):
    from ingest import write_extra_tables

    bundle, tables, repo, writer, sample, layouts = _extra_run(tmp_path)
    write_extra_tables(writer, bundle, tables, sample, layouts, "r")
    for name in ("codewords", "diffexp_azimuth", "diffexp_graphclust"):
        text = tables.catalog.load_table(f"ns.{name}").properties["referenced_by"]
        assert "uint8" not in text and "iceberg_tables" in text and "repo r" in text


def test_a_run_that_stopped_after_the_tables_registered_resumes_to_one_snapshot_each(tmp_path, monkeypatch):
    import ingest

    bundle, tables, repo, writer, sample, layouts = _extra_run(tmp_path)
    real = ingest.table_attr

    def stops_at_the_last_table(tables, name, *args, **kwargs):
        if name == "diffexp_graphclust":
            raise RuntimeError("stop")
        return real(tables, name, *args, **kwargs)

    monkeypatch.setattr(ingest, "table_attr", stops_at_the_last_table)
    with pytest.raises(RuntimeError, match="stop"):
        ingest.write_extra_tables(writer, bundle, tables, sample, layouts, "r")
    assert not writer.has_attribute(ingest.ICEBERG_TABLES)
    assert len(tables.catalog.load_table("ns.diffexp_graphclust").metadata.snapshots) == 1
    monkeypatch.setattr(ingest, "table_attr", real)
    ingest.write_extra_tables(writer, bundle, tables, sample, layouts, "r")
    assert writer.has_attribute(ingest.ICEBERG_TABLES)
    for name in ("codewords", "diffexp_azimuth", "diffexp_graphclust"):
        assert len(tables.catalog.load_table(f"ns.{name}").metadata.snapshots) == 1


def _matrix_zip(tmp_path):
    import pandas as pd
    import scipy.sparse
    from anndata.io import write_elem

    from .stores import zip_directory

    source = tmp_path / "matrix.zarr"
    root = zarr.open_group(source, mode="w", zarr_format=2)
    write_elem(root, "X", scipy.sparse.csr_matrix(np.array([[1, 0], [0, 2], [3, 3]], dtype="int32")))
    # obs.cell_id packs the cells store's pairs (1, 1), (2, 1), (3, 1) as prefix + suffix * 2**32.
    write_elem(root, "obs", pd.DataFrame({"cell_id": [1 + 2**32, 2 + 2**32, 3 + 2**32]},
                                         index=pd.Index(["a", "b", "c"], name="barcode")))
    write_elem(root, "var", pd.DataFrame({"n": [1, 2]}, index=pd.Index(["g1", "g2"], name="feature_id")))
    return zip_directory(source, tmp_path / "matrix.zip")


def test_the_table_annotates_cell_boundaries_with_one_instance_per_row(tmp_path):
    from anndata.io import read_elem
    from ingest import write_table
    from minus80.core.elements import ElementWriter
    from minus80.core.icechunk_io import create_repo

    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    write_table(ElementWriter(repo, "fixture"), _Bundle(_matrix_zip(tmp_path)))
    group = zarr.open_group(repo.readonly_session(branch="fixture").store, mode="r")["tables/table"]
    assert group.attrs["region"] == "cell_boundaries"
    assert group["uns/spatialdata_attrs/region"][()] == "cell_boundaries"
    assert group["obs/instance"][:].tolist() == [1, 2, 3]
    assert set(read_elem(group["obs/region"])) == {"cell_boundaries"}


def test_a_fresh_fixture_has_a_table_region_that_every_gate_of_the_region_accepts(tmp_path):
    from ingest import FIXTURE_TABLES, CELL_SET, NUCLEUS_SET, table_specs, write_boundaries, write_table
    from minus80.core.elements import ElementWriter
    from minus80.core.icechunk_io import create_repo
    from verify import gate_table_region

    class FixtureBundle:
        def __init__(self, matrix, cells):
            self.zips = {"cell_feature_matrix.zarr.zip": matrix, "cells.zarr.zip": cells}

        def member_zip(self, name):
            return self.zips[name]

    assert "cell_boundaries" in FIXTURE_TABLES
    bundle = FixtureBundle(_matrix_zip(tmp_path), cells_zip(tmp_path))
    tables = TableWriter(local_catalog(tmp_path / "catalog"), "ns", catalog_name="local", org="local")
    specs = table_specs({}, ["a", "b", "c", "d", "e"])
    for name in FIXTURE_TABLES:
        tables.require(name, **specs[name])
    repo = create_repo(tmp_path / "repo", [], prefixes=[tables.data_prefix(n) for n in FIXTURE_TABLES])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    writer = ElementWriter(repo, "fixture")
    write_table(writer, bundle)
    write_boundaries(writer, bundle, tables, "nucleus_boundaries", NUCLEUS_SET)
    write_boundaries(writer, bundle, tables, "cell_boundaries", CELL_SET)
    with zarr.config.set({"array.rectilinear_chunks": True}):
        assert gate_table_region(_pinned(repo), tables.catalog) == []
