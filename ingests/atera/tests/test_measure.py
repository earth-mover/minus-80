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
    assert f"part {part}: concurrency 2, limit {limit}" in lines
    repo = create_repo(tmp_path / "repo", [], open_existing=True)
    assert repo.lookup_branch("measure-test") == result["snapshot"]
    assert repo.lookup_branch("main") != result["snapshot"]


def test_the_limit_defaults_cover_a_whole_small_source(monkeypatch, capsys, sources, tmp_path):
    _, _, result = _run(monkeypatch, capsys, sources, tmp_path, "plane")
    assert (result["limit"], result["count"]) == (300, 6)


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


def _all(monkeypatch, capfd, sources, tmp_path, *extra):
    argv = ["measure.py", "--local", str(tmp_path / "repo"), "--bundle", str(sources / "outs.zip"),
            "--he-image", str(sources / "he.ome.tif"), "--branch", "measure-test", "--part", "all", *extra]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        measure.main()
    out = capfd.readouterr().out
    return raised.value.code, out, [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_all_runs_each_part_in_its_own_process_and_the_tile_last(monkeypatch, capfd, sources, tmp_path):
    code, _, results = _all(monkeypatch, capfd, sources, tmp_path, "--concurrency", "1", "2")
    assert code == 0
    assert [(r["part"], r["concurrency"], r["check"]) for r in results] == [
        *((p, n, "PASS") for n in (1, 2) for p in measure.PARTS[:-1]), ("tile", None, "PASS")]


def test_all_goes_on_after_a_failed_part_and_says_which_failed(monkeypatch, capfd, sources, tmp_path):
    code, out, results = _all(monkeypatch, capfd, sources, tmp_path, "--tile", "9,9")
    assert code == 1
    assert [r["part"] for r in results] == list(measure.PARTS[:-1])
    assert "part tile failed with exit code 1" in out


def test_a_tile_block_numbers_its_tiles_from_its_own_origin(tmp_path):
    import numpy as np

    data = np.arange(40 * 50, dtype=np.uint16).reshape(40, 50)
    with _plane(tmp_path, data) as tif:
        page = tif.pages.first
        block = measure.TileBlock(page, row=1, column=1, positions=2)
        assert (block.imagelength, block.imagewidth, block.tilelength) == (16, 32, 16)
        assert list(block.dataoffsets) == list(page.dataoffsets[5:7])
        fh = page.parent.filehandle
        fh.seek(block.dataoffsets[1])
        segment, indices, _ = block.decode(fh.read(block.databytecounts[1]), 1)
        assert indices[2:4] == (0, 16)
        assert np.array_equal(segment[0, :, :, 0], data[16:32, 32:48])


def _plane(tmp_path, data):
    tifffile.imwrite(tmp_path / "plane.tif", data, tile=(16, 16), compression="zlib")
    return tifffile.TiffFile(tmp_path / "plane.tif")


def test_a_window_of_tiles_yields_those_tiles_and_no_others(tmp_path):
    import numpy as np

    data = np.arange(40 * 50, dtype=np.uint16).reshape(40, 50)
    with _plane(tmp_path, data) as tif:
        page = tif.pages.first
        window = list(measure.TileWindow(page, 5, 3).segments())
        # The page has 3 rows of 4 tiles; tiles 5, 6 and 7 are the row 1, columns 1 to 3.
        assert [(r[1][2], r[1][3]) for r in window] == [(16, 16), (16, 32), (16, 48)]
        assert np.array_equal(window[0][0][0, :, :, 0], data[16:32, 16:32])


def test_the_tiles_come_from_the_middle_of_the_level():
    assert [measure.middle_start(total, n) for total, n in [(1848, 300), (6, 2), (6, 400), (10, 10), (5, 1)]] == [
        774, 2, 0, 0, 2]


def test_a_tile_of_fill_values_stores_no_chunk_and_the_result_counts_it(tmp_path):
    import numpy as np

    from minus80.core.remote import ReadCounter

    data = np.ones((40, 50), dtype=np.uint16)
    data[16:32, 16:48] = 0
    repo = _repo(tmp_path)
    with _plane(tmp_path, data) as tif:
        page = tif.pages.first
        result = measure.measure_plane(repo, "measure-t", "measure/p", page, ReadCounter(), tiles=4, start=4)
        assert measure.check_plane(repo, result, page, 4, 4) == []
    assert (result["count"], result["store_sets"], result["empty_chunks"], result["start"]) == (4, 2, 2, 4)


def _repo(tmp_path):
    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("measure-t", repo.lookup_branch("main"))
    return repo


def test_the_stack_and_the_mask_come_from_the_middle(monkeypatch, capsys, sources, tmp_path):
    # The synthetic stack has 2 tile rows of 3 columns, and the mask 4 chunks.
    _, _, stack = _run(monkeypatch, capsys, sources, tmp_path, "stack", "--limit", "1", repo="a")
    assert (stack["row"], stack["column"], stack["check"]) == (1, 1, "PASS")
    _, _, mask = _run(monkeypatch, capsys, sources, tmp_path, "mask", "--limit", "1", repo="b")
    assert (mask["start"], mask["check"]) == (2, "PASS")
    _, _, plane = _run(monkeypatch, capsys, sources, tmp_path, "plane", "--limit", "2", repo="c")
    assert (plane["start"], plane["check"]) == (2, "PASS")
    assert plane["empty_chunks"] >= 0
