"""``--replace-tables``: the full ingest replaces the rows of two held tables in place."""

import hashlib
import sys

import pyarrow as pa
import pytest
import zarr

import ingest
import verify
from minus80.core.elements import ElementWriter
from minus80.core.icechunk_io import create_repo
from minus80.core.registry import file_path
from minus80.core.tables import TableWriter, declare_copy, local_catalog
from bundle import CELLS, TRANSCRIPTS
from ingest import (
    NUCLEUS_SET, REPLACEABLE, replace_table_names, require_tables, table_attr, table_specs, write_boundaries,
    write_transcripts,
)
from polygons import BBOX, GEO_POLYGONS
from tests.stores import cells_zip
from tests.test_ingest_tables import _hold, _transcripts_run, _transcripts_zip

METHODS = ["a", "b", "c", "d", "e"]
#: Tile ``-1,0`` first, so tile ``0,0`` starts at ``instance`` 1, as on the full sample.
PLAN_TILES = ["-1,0", "0,0"]
#: The columns of ``nucleus_boundaries`` on the fixture branch: one row per part, with ``method``.
STAGE_ONE_BOUNDARIES = pa.schema([
    pa.field("instance", pa.int64(), nullable=False), ("cell_index", pa.int64()), ("cell_id", pa.string()),
    ("method", pa.int64()), ("geometry", pa.binary()), ("bbox", BBOX),
])


@pytest.fixture(autouse=True)
def rectilinear_chunks():
    with zarr.config.set({"array.rectilinear_chunks": True}):
        yield


class _FullBundle:
    """The cells and the transcripts zips of one synthetic sample."""

    def __init__(self, tmp_path):
        self.zips = {CELLS: cells_zip(tmp_path), TRANSCRIPTS: _transcripts_zip(tmp_path)}
        self.zf = self.zips[TRANSCRIPTS]

    def member_zip(self, name):
        return self.zips[name]


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _boundary_rows():
    box = {"xmin": 0.0, "ymin": 0.0, "xmax": 1.0, "ymax": 1.0}
    return pa.table({
        "instance": [1, 3, 3], "cell_index": [0, 2, 2], "cell_id": ["aaaaaaab-1", "aaaaaaad-1", "aaaaaaad-1"],
        "method": [3, 3, 4], "geometry": [b"a", b"b", b"c"], "bbox": [box] * 3,
    }, schema=STAGE_ONE_BOUNDARIES)


def _stage_one(tmp_path):
    """The fixture's two tables and elements, built by hand, and a repo whose container covers all three tables.

    Returns the bundle, the catalog, the repo, and each table's pinned reference.
    """
    bundle = _FullBundle(tmp_path)
    tables = TableWriter(local_catalog(tmp_path / "catalog"), "ns", catalog_name="local", org="local")
    specs = table_specs({}, METHODS)
    tables.require("nucleus_boundaries", schema=STAGE_ONE_BOUNDARIES, properties={})
    tables.require("transcripts", **specs["transcripts"])
    tables.require("cell_boundaries", **specs["cell_boundaries"])
    prefixes = [tables.data_prefix(n) for n in ("nucleus_boundaries", "transcripts", "cell_boundaries")]
    repo = create_repo(tmp_path / "repo", [], prefixes=prefixes)
    repo.create_branch("fixture", repo.lookup_branch("main"))
    repo.create_branch("full", repo.lookup_branch("main"))
    refs = {"nucleus_boundaries": tables.write("nucleus_boundaries", _boundary_rows(), geo=GEO_POLYGONS)}
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0)
    refs["transcripts"] = tables.ref("transcripts")
    writer = ElementWriter(repo, "fixture")
    for name, path, geo in (("nucleus_boundaries", "shapes", GEO_POLYGONS), ("transcripts", "points", None)):
        with writer.unit(f"{path}/{name}", f"Write {path}/{name}") as (root, _):
            group = root.require_group(path).create_group(name)
            group.attrs["iceberg_table"] = table_attr(tables, name, refs[name], geo=geo)
            declare_copy(group, f"{path}.parquet", refs[name].files)
    return bundle, tables.catalog, repo, refs


def _replace(catalog, repo, bundle, replace=REPLACEABLE):
    """One full ingest of the two tables onto the branch ``full``, from a writer of its own."""
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    require_tables(tables, table_specs({}, METHODS), set(replace))
    writer = ElementWriter(repo, "full")
    write_boundaries(writer, bundle, tables, "nucleus_boundaries", NUCLEUS_SET, replace="nucleus_boundaries" in replace)
    write_transcripts(writer, bundle, tables, tiles=PLAN_TILES, replace_held="transcripts" in replace)
    return tables


def _snapshots(catalog, name):
    return len(catalog.load_table(f"ns.{name}").metadata.snapshots)


def _fixture_root(repo):
    return zarr.open_group(repo.readonly_session(branch="fixture").store, mode="r")


def test_the_opt_in_names_only_tables_that_the_full_mode_replaces():
    assert replace_table_names("transcripts,nucleus_boundaries") == {"transcripts", "nucleus_boundaries"}
    assert set(REPLACEABLE) == {"transcripts", "nucleus_boundaries"}
    with pytest.raises(ValueError, match="cell_labels"):
        replace_table_names("transcripts,cell_labels")


def test_the_opt_in_is_refused_in_fixture_mode_and_for_an_unknown_table(monkeypatch, tmp_path):
    def no_bundle(location):
        raise AssertionError("main() opened the bundle")

    monkeypatch.setattr(ingest, "open_bundle", no_bundle)
    monkeypatch.chdir(tmp_path)
    base = ["ingest.py", "--sample", "kidney", "--repo", "r", "--catalog", "c", "--namespace", "n"]
    monkeypatch.setattr(sys, "argv", [*base, "--mode", "fixture", "--replace-tables", "transcripts"])
    with pytest.raises(SystemExit, match="--replace-tables is an option of --mode full"):
        ingest.main()
    monkeypatch.setattr(sys, "argv", [*base, "--mode", "full", "--branch", "main", "--replace-tables", "cell_labels"])
    with pytest.raises(SystemExit, match="cell_labels"):
        ingest.main()


def test_without_the_opt_in_a_table_with_other_columns_is_refused_and_unchanged(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    with pytest.raises(ValueError, match=r"ns\.nucleus_boundaries differs.*adds part_count, methods.*deletes method"):
        require_tables(tables, table_specs({}, METHODS), set())
    assert [f.name for f in catalog.load_table("ns.nucleus_boundaries").schema().fields][3] == "method"
    assert _snapshots(catalog, "nucleus_boundaries") == 1


def test_without_the_opt_in_a_held_tile_from_another_origin_is_refused_and_stays(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    tables.require("transcripts", **table_specs({}, METHODS)["transcripts"])
    with pytest.raises(ValueError, match=r"tile 0,0: .*0\.\.4.*1\.\.5"):
        write_transcripts(ElementWriter(repo, "full"), bundle, tables, tiles=PLAN_TILES)
    # The tile before it in the plan is written; the held tile is not touched.
    assert tables.partition_ranges("transcripts", "instance")["0,0"] == (0, 4)
    assert tables.partitions("transcripts") == {"0,0": 5, "-1,0": 1}


def test_the_opt_in_evolves_the_columns_and_prints_one_line(tmp_path, capsys):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    capsys.readouterr()
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    require_tables(tables, table_specs({}, METHODS), {"nucleus_boundaries"})
    table = catalog.load_table("ns.nucleus_boundaries")
    assert [f.name for f in table.schema().fields] == ["instance", "cell_index", "cell_id", "geometry", "bbox", "part_count", "methods"]
    assert table.schema().find_field("part_count").field_id > 6
    lines = [line for line in capsys.readouterr().out.splitlines() if "nucleus_boundaries" in line]
    assert lines == ["nucleus_boundaries: columns added: part_count, methods; columns deleted: method"]
    require_tables(tables, table_specs({}, METHODS), {"nucleus_boundaries"})
    assert len(catalog.load_table("ns.nucleus_boundaries").metadata.schemas) == len(table.metadata.schemas)
    assert capsys.readouterr().out == ""


def test_the_replace_gives_nucleus_boundaries_one_row_per_cell_and_keeps_the_earlier_rows(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    earlier = refs["nucleus_boundaries"]
    kept = {f.location: _digest(file_path(f.location)) for f in earlier.files}
    _replace(catalog, repo, bundle)
    table = catalog.load_table("ns.nucleus_boundaries")
    now = table.scan().to_arrow().sort_by("instance")
    assert now.column_names == ["instance", "cell_index", "cell_id", "geometry", "bbox", "part_count", "methods"]
    assert now["instance"].to_pylist() == [1, 3] and now["part_count"].to_pylist() == [1, 2]
    footer = zarr.open_group(repo.readonly_session(branch="full").store, mode="r")["shapes/nucleus_boundaries"]
    assert footer.attrs["iceberg_table"]["snapshot_id"] == table.current_snapshot().snapshot_id
    before = table.scan(snapshot_id=earlier.snapshot_id).to_arrow().sort_by("instance")
    assert before.column_names == ["instance", "cell_index", "cell_id", "method", "geometry", "bbox"]
    assert before["instance"].to_pylist() == [1, 3, 3] and before["method"].to_pylist() == [3, 3, 4]
    assert {f.location: _digest(file_path(f.location)) for f in earlier.files} == kept
    assert [t.file.file_path for t in table.scan().plan_files()] != [f.location for f in earlier.files]


def test_the_replace_of_transcripts_keeps_the_earlier_tile(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    earlier = refs["transcripts"]
    kept = {f.location: _digest(file_path(f.location)) for f in earlier.files}
    tables = _replace(catalog, repo, bundle)
    table = catalog.load_table("ns.transcripts")
    assert tables.partitions("transcripts") == {"-1,0": 1, "0,0": 5}
    assert sorted(table.scan().to_arrow()["instance"].to_pylist()) == list(range(6))
    assert sorted(table.scan(snapshot_id=earlier.snapshot_id).to_arrow()["instance"].to_pylist()) == list(range(5))
    assert {f.location: _digest(file_path(f.location)) for f in earlier.files} == kept


def test_the_gates_of_the_fixture_branch_pass_before_and_after_the_replace(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)

    def gates():
        root = _fixture_root(repo)
        return {"copies": verify.gate_copies(root, catalog), "attrs": verify.gate_attrs(root, catalog),
                "field_ids": verify.gate_field_ids(root, catalog)}

    assert gates() == {"copies": [], "attrs": [], "field_ids": []}
    _replace(catalog, repo, bundle)
    assert gates() == {"copies": [], "attrs": [], "field_ids": []}
    pinned = verify.pinned_columns(catalog, dict(_fixture_root(repo)["shapes/nucleus_boundaries"].attrs["iceberg_table"]),
                                   ("instance", "method"))
    assert pinned["method"].to_pylist() == [3, 3, 4]


def test_a_second_replace_changes_no_table(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    _replace(catalog, repo, bundle)
    state = {n: (catalog.load_table(f"ns.{n}").current_snapshot().snapshot_id, _snapshots(catalog, n),
                 len(catalog.load_table(f"ns.{n}").metadata.schemas))
             for n in ("nucleus_boundaries", "transcripts")}
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    # The elements of a second branch are new; the tables must stay as they are.
    repo.create_branch("again", repo.lookup_branch("main"))
    require_tables(tables, table_specs({}, METHODS), set(REPLACEABLE))
    write_boundaries(ElementWriter(repo, "again"), bundle, tables, "nucleus_boundaries", NUCLEUS_SET, replace=True)
    write_transcripts(ElementWriter(repo, "again"), bundle, tables, tiles=PLAN_TILES, replace_held=True)
    assert {n: (catalog.load_table(f"ns.{n}").current_snapshot().snapshot_id, _snapshots(catalog, n),
                len(catalog.load_table(f"ns.{n}").metadata.schemas)) for n in state} == state


def _rows(tables):
    return tables.catalog.load_table("ns.transcripts").scan().to_arrow().sort_by("instance")


def test_a_held_tile_from_another_origin_is_removed_and_written_again_in_one_snapshot(tmp_path):
    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=10)
    [wrong] = tables.ref("transcripts").files
    write_transcripts(writer, bundle, tables, replace_held=True)
    table = tables.catalog.load_table("ns.transcripts")
    assert tables.partition_ranges("transcripts", "instance") == {"0,0": (0, 4), "-1,0": (5, 5)}
    # The hold, the replaced tile 0,0 and the new tile -1,0.
    assert len(table.metadata.snapshots) == 3
    assert wrong.location not in {t.file.file_path for t in table.scan().plan_files()}
    assert file_path(wrong.location).exists()
    assert sorted(table.scan(snapshot_id=table.metadata.snapshots[0].snapshot_id).to_arrow()["instance"].to_pylist()) \
        == list(range(10, 15))


def test_a_held_tile_with_another_row_count_is_replaced(tmp_path):
    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, rows=2)
    write_transcripts(writer, bundle, tables, replace_held=True)
    assert tables.partitions("transcripts") == {"-1,0": 1, "0,0": 5}


def test_a_correct_held_tile_is_kept_and_not_written_again(tmp_path, capsys):
    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0)
    [held] = tables.ref("transcripts").files
    stamp = _digest(file_path(held.location))
    write_transcripts(writer, bundle, tables, replace_held=True)
    assert "tile 0,0: in the table already, skipped" in capsys.readouterr().out
    table = tables.catalog.load_table("ns.transcripts")
    assert held.location in {t.file.file_path for t in table.scan().plan_files()}
    assert _digest(file_path(held.location)) == stamp
    assert len(table.metadata.snapshots) == 2


def test_a_partition_outside_the_plan_is_removed_in_its_own_snapshot(tmp_path):
    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, name="9,9")
    [stray] = tables.ref("transcripts", partition_order=["9,9"]).files
    write_transcripts(writer, bundle, tables, replace_held=True)
    table = tables.catalog.load_table("ns.transcripts")
    assert tables.partitions("transcripts") == {"-1,0": 1, "0,0": 5}
    # The hold, the removal, and one snapshot for each of the two tiles.
    assert len(table.metadata.snapshots) == 4
    assert file_path(stray.location).exists()


@pytest.mark.parametrize("stop_at", [1, 2])
def test_a_rerun_after_a_stop_ends_with_the_table_of_an_unstopped_run(tmp_path, monkeypatch, stop_at):
    def prepared(directory):
        directory.mkdir()
        run = _transcripts_run(directory)
        # A wrong tile and a tile outside the plan.
        _hold(run[1], run[0], directory, "0,0", first_instance=10)
        _hold(run[1], run[0], directory, "0,0", first_instance=0, name="9,9")
        return run

    whole = prepared(tmp_path / "whole")
    write_transcripts(whole[3], whole[0], whole[1], replace_held=True)

    bundle, tables, repo, writer = prepared(tmp_path / "stopped")
    real, calls = ingest.write_tile, []

    def stops(*args, **kwargs):
        calls.append(args[1])
        if len(calls) == stop_at:
            raise RuntimeError("stop")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "write_tile", stops)
    with pytest.raises(RuntimeError, match="stop"):
        write_transcripts(writer, bundle, tables, replace_held=True)
    monkeypatch.setattr(ingest, "write_tile", real)
    write_transcripts(writer, bundle, tables, replace_held=True)
    assert _rows(tables).equals(_rows(whole[1]))
    assert tables.partition_ranges("transcripts", "instance") == whole[1].partition_ranges("transcripts", "instance")
    assert tables.partitions("transcripts") == {"-1,0": 1, "0,0": 5}
    assert writer.has("points/transcripts")


def test_a_stop_after_the_removal_leaves_the_tile_absent_so_the_rerun_writes_it(tmp_path, monkeypatch):
    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, name="9,9")

    def stops(*args, **kwargs):
        raise RuntimeError("stop")

    monkeypatch.setattr(ingest, "write_tile", stops)
    with pytest.raises(RuntimeError, match="stop"):
        write_transcripts(writer, bundle, tables, replace_held=True)
    assert tables.partitions("transcripts") == {}
    monkeypatch.undo()
    write_transcripts(writer, bundle, tables, replace_held=True)
    assert tables.partitions("transcripts") == {"-1,0": 1, "0,0": 5}
