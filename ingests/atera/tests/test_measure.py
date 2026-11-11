import json
import sys

import pyarrow.parquet as pq
import pytest
import tifffile

import measure
from minus80.core.icechunk_io import create_repo
from tests.test_images import _rgb_pyramid, write_bundle
from tests.test_verify import _bundle_members

KEYS = {
    "part", "concurrency", "limit", "count", "unit", "wall_s", "cpu_s", "peak_rss_mib", "read_bytes",
    "source_requests", "stored_bytes", "store_sets", "write_s", "commit_s", "rate", "rate_unit", "machine",
    "cpu_count", "snapshot", "check",
}


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    """A synthetic bundle and a synthetic H&E image on disk."""
    directory = tmp_path_factory.mktemp("sources")
    write_bundle(directory / "outs.zip", _bundle_members(directory))
    _rgb_pyramid(directory / "he.ome.tif", planar=False)
    return directory


def _run(monkeypatch, capsys, sources, tmp_path, part, *args, branch="measure-test", repo="repo"):
    argv = ["measure.py", "--local", str(tmp_path / repo), "--bundle", str(sources / "outs.zip"),
            "--he-image", str(sources / "he.ome.tif"), "--branch", branch, "--part", part, *args]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        measure.main()
    lines = capsys.readouterr().out.strip().splitlines()
    return raised.value.code, lines, json.loads(lines[-1])


def test_sampled_spreads_over_the_items_and_keeps_both_ends():
    assert measure.sampled(list(range(10)), 4) == [0, 3, 6, 9]
    assert measure.sampled([1, 2], 4) == [1, 2]


@pytest.mark.parametrize("branch", ["main", "fixture", "scratch"])
def test_a_branch_that_does_not_start_with_measure_is_refused_before_anything_opens(
        monkeypatch, tmp_path, branch):
    argv = ["measure.py", "--local", str(tmp_path / "repo"), "--bundle", str(tmp_path / "absent.zip"),
            "--branch", branch, "--part", "plane"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        measure.main()
    assert "measure-" in str(raised.value)
    assert not (tmp_path / "repo").exists()


@pytest.mark.parametrize("part, limit, count, sets", [
    # The synthetic plane has 2 x 3 tiles; the H&E has 3 channels for each tile; the stack has 3 positions.
    ("plane", 4, 4, 4),
    ("mask", 3, 3, 3),
    ("stack", 2, 2, 2),
    ("he", 4, 4, 12),
])
def test_a_part_writes_only_its_limit_and_its_check_passes(
        monkeypatch, capsys, sources, tmp_path, part, limit, count, sets):
    code, lines, result = _run(monkeypatch, capsys, sources, tmp_path, part, "--limit", str(limit),
                               "--concurrency", "2")

    assert code == 0
    assert KEYS <= set(result)
    assert (result["part"], result["concurrency"], result["limit"]) == (part, 2, limit)
    assert (result["count"], result["store_sets"]) == (count, sets)
    assert result["check"] == "PASS"
    assert result["peak_rss_mib"] > 0 and result["snapshot"] and result["wall_s"] > 0
    assert any(line.startswith(f"{part}: PASS") for line in lines)
    repo = create_repo(tmp_path / "repo", [], open_existing=True)
    assert repo.lookup_branch("measure-test") == result["snapshot"]
    assert repo.lookup_branch("main") != result["snapshot"]


def test_the_limit_defaults_cover_a_whole_small_source(monkeypatch, capsys, sources, tmp_path):
    _, _, result = _run(monkeypatch, capsys, sources, tmp_path, "plane")
    assert (result["limit"], result["count"]) == (400, 6)


def test_a_part_writes_under_its_own_group_of_the_scratch_branch(monkeypatch, capsys, sources, tmp_path):
    import zarr

    _run(monkeypatch, capsys, sources, tmp_path, "mask", "--concurrency", "3")
    root = zarr.open_group(create_repo(tmp_path / "repo", [], open_existing=True)
                           .readonly_session(branch="measure-test").store, mode="r")
    assert [path for path, _ in root["measure"].groups()] == ["mask-c3"]


def test_the_tile_part_writes_the_largest_tile_to_a_local_parquet_file(monkeypatch, capsys, sources, tmp_path):
    scratch = tmp_path / "scratch"
    code, _, result = _run(monkeypatch, capsys, sources, tmp_path, "tile", "--scratch", str(scratch))

    file = scratch / "tile-0,0.parquet"
    assert code == 0 and KEYS <= set(result)
    assert (result["count"], result["unit"], result["concurrency"]) == (5, "rows", None)
    assert pq.ParquetFile(file).metadata.num_rows == 5
    assert result["parquet_bytes"] == file.stat().st_size
    assert pq.read_table(file)["instance"].to_pylist() == [0, 1, 2, 3, 4]
    assert result["snapshot"] is None and result["store_sets"] == 0
    assert not (tmp_path / "repo").exists()


def test_the_tile_part_refuses_a_tile_the_source_lacks(monkeypatch, capsys, sources, tmp_path):
    monkeypatch.setattr(sys, "argv", ["measure.py", "--local", str(tmp_path / "r"), "--bundle",
                                      str(sources / "outs.zip"), "--branch", "measure-t", "--part", "tile",
                                      "--tile", "9,9"])
    with pytest.raises(SystemExit, match="9,9"):
        measure.main()


def test_a_wrong_row_count_fails_the_tile_check(tmp_path):
    import pyarrow as pa

    file = tmp_path / "t.parquet"
    pq.write_table(pa.table({"instance": pa.array([0, 1, 5], pa.int64())}), file)
    assert measure.check_tile(file, 3) == ["instance runs 0..5, not 0..2"]
    assert measure.check_tile(file, 4) == ["the file has 3 rows, the source 4", "instance runs 0..5, not 0..3"]


def test_all_runs_each_part_in_its_own_process(monkeypatch, capfd, sources, tmp_path):
    argv = ["measure.py", "--local", str(tmp_path / "repo"), "--bundle", str(sources / "outs.zip"),
            "--he-image", str(sources / "he.ome.tif"), "--branch", "measure-test", "--part", "all"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        measure.main()
    results = [json.loads(line) for line in capfd.readouterr().out.splitlines() if line.startswith("{")]
    assert raised.value.code == 0
    assert [(r["part"], r["check"]) for r in results] == [(p, "PASS") for p in measure.PARTS]


def test_a_subset_page_yields_its_first_tiles_and_closes_its_source(tmp_path):
    import numpy as np

    tifffile.imwrite(tmp_path / "p.tif", np.arange(40 * 50, dtype=np.uint16).reshape(40, 50), tile=(16, 16))
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        page = tif.pages.first
        assert len(list(measure.FirstTiles(page, 5).segments())) == 5
        corner = measure.Corner(page, 16, 32)
        assert (corner.imagelength, corner.imagewidth, corner.tilelength) == (16, 32, 16)
