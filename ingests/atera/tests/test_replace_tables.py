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
from minus80.core.tables import HeldFilesError, TableWriter, declare_copy, local_catalog
from bundle import CELLS, TRANSCRIPTS
from ingest import (
    NUCLEUS_SET, REPLACEABLE, preflight, replace_table_names, require_tables, table_attr, table_specs, write_boundaries,
    write_transcripts,
)
from polygons import BBOX, GEO_POLYGONS
from ingest import CELL_SET, check_replace_elements, held_element_conflicts, write_extra_tables
from tests.stores import cells_zip
from tests.test_ingest_tables import _ExtraBundle, _extra_run, _hold, _transcripts_run, _transcripts_zip

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


def test_the_opt_in_names_every_table_that_the_full_mode_writes():
    assert replace_table_names("transcripts,nucleus_boundaries") == {"transcripts", "nucleus_boundaries"}
    assert set(REPLACEABLE) == {"transcripts", "nucleus_boundaries", "cell_boundaries", "codewords",
                                "diffexp_azimuth", "diffexp_graphclust"}
    assert replace_table_names("cell_boundaries,codewords") == {"cell_boundaries", "codewords"}
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


def test_without_the_opt_in_every_fault_is_refused_before_the_first_write(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    tables.require("transcripts", **table_specs({}, METHODS)["transcripts"])
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, name="9,9")
    with pytest.raises(ValueError, match=r"tile 0,0: the table holds 5 rows, instances 0\.\.4; expected 5 rows, instances 1\.\.5.*"
                                         r"tiles outside the plan: 9,9") as e:
        write_transcripts(ElementWriter(repo, "full"), bundle, tables, tiles=PLAN_TILES)
    assert "-1,0" not in str(e.value)
    assert tables.partitions("transcripts") == {"0,0": 5, "9,9": 5}
    assert _snapshots(catalog, "transcripts") == 2


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


def _window(tmp_path):
    """The two fixture tables with the schema commit of ``nucleus_boundaries`` done and its rows not replaced."""
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    require_tables(tables, table_specs({}, METHODS), {"nucleus_boundaries"})
    return bundle, catalog, repo, refs


def test_a_rerun_with_the_flag_after_the_schema_commit_replaces_the_rows(tmp_path):
    bundle, catalog, repo, refs = _window(tmp_path)
    _replace(catalog, repo, bundle)
    now = catalog.load_table("ns.nucleus_boundaries").scan().to_arrow().sort_by("instance")
    assert now["instance"].to_pylist() == [1, 3] and now["part_count"].to_pylist() == [1, 2]


def test_a_rerun_without_the_flag_after_the_schema_commit_raises_and_adds_no_snapshot(tmp_path):
    bundle, catalog, repo, refs = _window(tmp_path)
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    require_tables(tables, table_specs({}, METHODS), set())
    with pytest.raises(HeldFilesError, match=r"ns\.nucleus_boundaries holds 1 data file of other content$"):
        write_boundaries(ElementWriter(repo, "full"), bundle, tables, "nucleus_boundaries", NUCLEUS_SET)
    assert _snapshots(catalog, "nucleus_boundaries") == 1


def test_an_equal_rerun_without_the_flag_is_a_no_op(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    _replace(catalog, repo, bundle)
    repo.create_branch("again", repo.lookup_branch("main"))
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    require_tables(tables, table_specs({}, METHODS), set())
    write_boundaries(ElementWriter(repo, "again"), bundle, tables, "nucleus_boundaries", NUCLEUS_SET)
    assert _snapshots(catalog, "nucleus_boundaries") == 2


def test_the_flag_needs_the_element_of_each_table_in_the_run(monkeypatch, tmp_path):
    from ingest import check_replace_elements

    with pytest.raises(ValueError, match=r"nucleus_boundaries.*shapes/nucleus_boundaries"):
        check_replace_elements({"nucleus_boundaries"}, ["points/transcripts"])
    check_replace_elements({"nucleus_boundaries"}, ["shapes/nucleus_boundaries", "root"])

    def no_bundle(location):
        raise AssertionError("main() opened the bundle")

    monkeypatch.setattr(ingest, "open_bundle", no_bundle)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ingest.py", "--sample", "kidney", "--repo", "r", "--catalog", "c", "--namespace", "n",
                                      "--mode", "full", "--branch", "main", "--replace-tables", "nucleus_boundaries",
                                      "--elements", "points/transcripts"])
    with pytest.raises(SystemExit, match="shapes/nucleus_boundaries"):
        ingest.main()
    assert not (tmp_path / "c").exists()


def test_a_stop_inside_the_overwrite_of_nucleus_boundaries_leaves_the_table_and_the_rerun_ends_right(tmp_path, monkeypatch):
    import pyiceberg.io.pyarrow as pyarrow_io

    bundle, catalog, repo, refs = _window(tmp_path)
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    require_tables(tables, table_specs({}, METHODS), {"nucleus_boundaries"})
    real = pyarrow_io.parquet_file_to_data_file

    def stops(*args, **kwargs):
        raise RuntimeError("stop")

    monkeypatch.setattr(pyarrow_io, "parquet_file_to_data_file", stops)
    with pytest.raises(RuntimeError, match="stop"):
        write_boundaries(ElementWriter(repo, "full"), bundle, tables, "nucleus_boundaries", NUCLEUS_SET, replace=True)
    table = catalog.load_table("ns.nucleus_boundaries")
    assert len(table.metadata.snapshots) == 1 and table.current_snapshot().snapshot_id == refs["nucleus_boundaries"].snapshot_id
    monkeypatch.setattr(pyarrow_io, "parquet_file_to_data_file", real)
    _replace(catalog, repo, bundle)
    assert catalog.load_table("ns.nucleus_boundaries").scan().to_arrow().sort_by("instance")["instance"].to_pylist() == [1, 3]


def test_a_stop_inside_the_overwrite_of_a_transcripts_tile_leaves_the_table_and_the_rerun_ends_right(tmp_path, monkeypatch):
    import pyiceberg.io.pyarrow as pyarrow_io

    whole = tmp_path / "whole"
    whole.mkdir()
    bundle, tables, repo, writer = _transcripts_run(whole)
    _hold(tables, bundle, whole, "0,0", first_instance=10)
    stopped = tmp_path / "stopped"
    stopped.mkdir()
    bundle2, tables2, repo2, writer2 = _transcripts_run(stopped)
    _hold(tables2, bundle2, stopped, "0,0", first_instance=10)
    write_transcripts(writer, bundle, tables, replace_held=True)

    real = pyarrow_io.parquet_file_to_data_file

    def stops(*args, **kwargs):
        raise RuntimeError("stop")

    monkeypatch.setattr(pyarrow_io, "parquet_file_to_data_file", stops)
    with pytest.raises(RuntimeError, match="stop"):
        write_transcripts(writer2, bundle2, tables2, replace_held=True)
    assert tables2.partition_ranges("transcripts", "instance") == {"0,0": (10, 14)}
    assert _snapshots(tables2.catalog, "transcripts") == 1
    monkeypatch.setattr(pyarrow_io, "parquet_file_to_data_file", real)
    write_transcripts(writer2, bundle2, tables2, replace_held=True)
    assert _rows(tables2).equals(_rows(tables))


def test_the_transcripts_gate_reads_the_pinned_snapshot_after_the_replace(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    attr = dict(_fixture_root(repo)["points/transcripts"].attrs["iceberg_table"])
    grid = {"grid_keys": ["0,0"], "grid_number_objects": [5]}
    assert verify._transcript_errors("points/transcripts", catalog, attr, grid) == []
    _replace(catalog, repo, bundle)
    assert verify._transcript_errors("points/transcripts", catalog, attr, grid) == []


def test_the_transcripts_gate_looks_up_the_index_column_in_the_schema_of_the_pinned_snapshot(tmp_path):
    from minus80.core.tables import write_parquet

    old = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string()), ("old", pa.int64())])
    new = pa.schema([pa.field("instance", pa.int64(), nullable=False), ("grid_tile", pa.string()), ("new", pa.int64())])
    tables = TableWriter(local_catalog(tmp_path), "ns", catalog_name="local", org="local")
    tables.require("t", schema=old, properties={}, partition_by="grid_tile")
    path = tmp_path / "a.parquet"
    write_parquet(pa.table({"instance": [0, 1], "grid_tile": ["a", "a"], "old": [0, 1]}, schema=old), str(path),
                  index_column="instance", field_ids=tables.field_ids("t"))
    ref = tables.write_parts("t", [path])
    attr = table_attr(tables, "t", ref)
    attr["index_column"] = "old"
    tables.require("t", schema=new, properties={}, partition_by="grid_tile", evolve=True)
    grid = {"grid_keys": ["a"], "grid_number_objects": [2]}
    assert verify._transcript_errors("t", tables.catalog, attr, grid) == []


def _preflight(tmp_path, **kwargs):
    arguments = {"mode": "full", "branch": "main", "repo": str(tmp_path / "repo"), "catalog": str(tmp_path / "catalog"),
                 "namespace": "ns", "elements": None, "replace_tables": ""}
    return preflight(**{**arguments, **kwargs})


def _state(tmp_path):
    """What a preflight must not change: the tables with their metadata files, and the repo's branches."""
    from minus80.core.icechunk_io import open_repository

    catalog = local_catalog(tmp_path / "catalog")
    tables = {str(t): catalog.load_table(t).metadata_location for t in catalog.list_tables("ns")}
    return tables, sorted(open_repository(tmp_path / "repo").list_branches())


def test_a_preflight_of_a_clean_start_has_no_problem_and_creates_nothing(tmp_path, capsys):
    assert _preflight(tmp_path, branch="newbranch") == []
    out = capsys.readouterr().out
    assert "repo: absent" in out and "catalog: absent" in out
    assert list(tmp_path.iterdir()) == []


def test_a_preflight_of_the_held_tables_with_the_flag_plans_the_changes_and_changes_nothing(tmp_path, capsys):
    _stage_one(tmp_path)
    before = _state(tmp_path)
    capsys.readouterr()
    assert _preflight(tmp_path, replace_tables="transcripts,nucleus_boundaries", branch="new") == []
    out = capsys.readouterr().out
    assert "nucleus_boundaries: add part_count, methods; delete method; replace rows" in out
    assert "transcripts: 1 files held; held tiles are checked against the source at the transcripts step" in out
    assert "branch new: created from main by the ingest" in out
    assert "cell_boundaries: matches" in out
    assert _state(tmp_path) == before


def test_a_preflight_of_the_held_tables_without_the_flag_reports_the_difference_nothing_covers(tmp_path):
    _stage_one(tmp_path)
    before = _state(tmp_path)
    problems = _preflight(tmp_path)
    assert len(problems) == 1
    assert "nucleus_boundaries" in problems[0] and "adds part_count, methods" in problems[0] and "deletes method" in problems[0]
    assert _state(tmp_path) == before


def test_a_preflight_reports_each_argument_problem_in_one_line(tmp_path):
    problems = _preflight(tmp_path, branch="fixture", elements="images/nothing", replace_tables="cell_labels")
    assert len(problems) == 3
    assert any("fixture" in p for p in problems) and any("images/nothing" in p for p in problems)
    assert any("cell_labels" in p for p in problems)
    assert any("--preflight is an option of --mode full" in p for p in _preflight(tmp_path, mode="fixture"))
    rule = _preflight(tmp_path, elements="points/transcripts", replace_tables="nucleus_boundaries")
    assert len(rule) == 1 and "shapes/nucleus_boundaries" in rule[0]
    assert list(tmp_path.iterdir()) == []


def test_preflight_on_the_command_line_exits_with_the_number_of_the_result(monkeypatch, tmp_path):
    def no_bundle(location):
        raise AssertionError("main() opened the bundle")

    monkeypatch.setattr(ingest, "open_bundle", no_bundle)
    base = ["ingest.py", "--sample", "kidney", "--repo", str(tmp_path / "repo"), "--catalog", str(tmp_path / "catalog"),
            "--namespace", "ns", "--mode", "full", "--branch", "main", "--preflight"]
    monkeypatch.setattr(sys, "argv", base)
    with pytest.raises(SystemExit) as clean:
        ingest.main()
    monkeypatch.setattr(sys, "argv", [*base, "--elements", "images/nothing"])
    with pytest.raises(SystemExit) as wrong:
        ingest.main()
    assert (clean.value.code, wrong.value.code) == (0, 1)


def test_a_table_of_another_content_is_replaced_by_the_flag_the_message_names(tmp_path):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    for branch in ("one", "two", "three"):
        repo.create_branch(branch, repo.lookup_branch("main"))
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    require_tables(tables, table_specs({}, METHODS), {"nucleus_boundaries"})
    write_boundaries(ElementWriter(repo, "one"), bundle, tables, "cell_boundaries", CELL_SET)
    # The nucleus set has other rows than the cell set.
    with pytest.raises(HeldFilesError, match=r"ns\.cell_boundaries holds 1 data file of other content$"):
        write_boundaries(ElementWriter(repo, "two"), bundle, tables, "cell_boundaries", NUCLEUS_SET)
    write_boundaries(ElementWriter(repo, "three"), bundle, tables, "cell_boundaries", NUCLEUS_SET, replace=True)
    assert catalog.load_table("ns.cell_boundaries").scan().to_arrow()["instance"].to_pylist() == [1, 3]


class _OtherExtraBundle(_ExtraBundle):
    def member_file(self, name):
        import io

        return io.BytesIO(b"Feature ID,Feature Name,Amacrine cell Mean Counts\nENSG1,A,0.5\nENSG2,B,0.7\n")


def test_a_small_table_of_another_content_is_replaced_by_the_flag_the_message_names(tmp_path):
    bundle, tables, repo, writer, sample, layouts = _extra_run(tmp_path)
    write_extra_tables(writer, bundle, tables, sample, layouts, "r")
    repo.create_branch("again", repo.lookup_branch("main"))
    other, again = _OtherExtraBundle(tmp_path), ElementWriter(repo, "again")
    with pytest.raises(HeldFilesError, match=r"ns\.diffexp_azimuth holds 1 data file of other content$"):
        write_extra_tables(again, other, tables, sample, layouts, "r")
    write_extra_tables(again, other, tables, sample, layouts, "r", {"diffexp_azimuth", "diffexp_graphclust"})
    assert tables.catalog.load_table("ns.diffexp_azimuth").scan().to_arrow().num_rows == 2
    assert tables.catalog.load_table("ns.codewords").current_snapshot().snapshot_id is not None
    assert len(tables.catalog.load_table("ns.codewords").metadata.snapshots) == 1


def test_the_element_rule_covers_every_table_of_the_full_mode():
    with pytest.raises(ValueError, match=r"codewords.*iceberg_tables"):
        check_replace_elements({"codewords"}, ["shapes/cell_boundaries"])
    with pytest.raises(ValueError, match=r"cell_boundaries.*shapes/cell_boundaries"):
        check_replace_elements({"cell_boundaries"}, ["points/transcripts"])
    check_replace_elements({"codewords", "diffexp_azimuth"}, ["iceberg_tables"])


def test_a_preflight_lists_the_files_and_rows_of_a_held_single_write_table(tmp_path, capsys):
    bundle, catalog, repo, refs = _stage_one(tmp_path)
    tables = TableWriter(catalog, "ns", catalog_name="local", org="local")
    tables.require("cell_boundaries", **table_specs({}, METHODS)["cell_boundaries"])
    repo.create_branch("again", repo.lookup_branch("main"))
    write_boundaries(ElementWriter(repo, "again"), bundle, tables, "cell_boundaries", CELL_SET)
    capsys.readouterr()
    assert _preflight(tmp_path, replace_tables="transcripts,nucleus_boundaries") == []
    assert ("cell_boundaries: matches; 1 files, 3 rows held; an equal-content rerun is a no-op, "
            "and different content needs --replace-tables cell_boundaries") in capsys.readouterr().out


def _hold_element(repo, branch, path):
    with ElementWriter(repo, branch).unit(path, f"Hold {path}") as (root, _):
        parent, name = path.split("/")
        root.require_group(parent).create_group(name)


def test_the_flag_with_a_held_element_and_a_changed_schema_is_refused_with_the_element_and_the_table(tmp_path):
    from minus80.core.icechunk_io import open_repository

    bundle, catalog, repo, refs = _stage_one(tmp_path)
    _hold_element(repo, "full", "shapes/nucleus_boundaries")
    _hold_element(repo, "full", "points/transcripts")
    specs = table_specs({}, METHODS)
    handle = open_repository(tmp_path / "repo")
    found = held_element_conflicts(catalog, "ns", specs, {"nucleus_boundaries", "transcripts"}, handle, "full")
    # Only the table with another schema conflicts; the transcripts schema matches.
    assert len(found) == 1 and "shapes/nucleus_boundaries" in found[0] and "nucleus_boundaries" in found[0]
    assert held_element_conflicts(catalog, "ns", specs, set(), handle, "full") == []
    assert held_element_conflicts(catalog, "ns", specs, {"nucleus_boundaries"}, handle, "new") == []
    problems = _preflight(tmp_path, branch="full", replace_tables="nucleus_boundaries")
    assert len(problems) == 1 and "shapes/nucleus_boundaries" in problems[0]
    state = _state(tmp_path)

    class Counter:
        bytes = 0
        wrap = None

    class StubBundle:
        pass

    def stub_prepare(*args, **kwargs):
        return ingest.Prepared(experiment={"pixel_size": 1.0}, root={}, methods=METHODS, qc={})

    import sys as _sys
    from unittest import mock

    argv = ["ingest.py", "--sample", "kidney", "--repo", str(tmp_path / "repo"), "--catalog", str(tmp_path / "catalog"),
            "--namespace", "ns", "--mode", "full", "--branch", "full", "--bundle", "b", "--replace-tables", "nucleus_boundaries"]
    with mock.patch.object(_sys, "argv", argv), mock.patch.object(ingest, "open_bundle", lambda location: (StubBundle(), Counter())), \
            mock.patch.object(ingest, "prepare", stub_prepare):
        with pytest.raises(SystemExit, match="shapes/nucleus_boundaries"):
            ingest.main()
    assert _state(tmp_path) == state
    assert [f.name for f in catalog.load_table("ns.nucleus_boundaries").schema().fields][3] == "method"


def test_the_flag_with_a_held_element_and_a_matching_schema_is_not_refused(tmp_path):
    from minus80.core.icechunk_io import open_repository

    bundle, catalog, repo, refs = _stage_one(tmp_path)
    _replace(catalog, repo, bundle)
    specs = table_specs({}, METHODS)
    handle = open_repository(tmp_path / "repo")
    assert held_element_conflicts(catalog, "ns", specs, set(REPLACEABLE), handle, "full") == []


def test_a_preflight_that_cannot_load_a_table_reports_the_table_and_the_exception_type_only(tmp_path, monkeypatch):
    from pyiceberg.catalog.sql import SqlCatalog

    _stage_one(tmp_path)

    def fails(self, identifier):
        raise RuntimeError(f"cannot read s3://bucket/{tmp_path}")

    monkeypatch.setattr(SqlCatalog, "load_table", fails)
    problems = _preflight(tmp_path, replace_tables="transcripts,nucleus_boundaries")
    assert {"cell_boundaries", "nucleus_boundaries", "transcripts"} <= {p.split(":")[0] for p in problems}
    assert all("RuntimeError" in p and "bucket" not in p and str(tmp_path) not in p for p in problems)


def test_a_fault_for_a_tile_with_no_rows_says_no_rows(tmp_path):
    bundle, tables, repo, writer = _transcripts_run(tmp_path)
    _hold(tables, bundle, tmp_path, "0,0", first_instance=0, name="1,0")
    with pytest.raises(ValueError, match=r"tile 1,0: the table holds 5 rows, instances 0\.\.4; expected no rows$") as e:
        write_transcripts(writer, bundle, tables)
    assert "-1" not in str(e.value)
