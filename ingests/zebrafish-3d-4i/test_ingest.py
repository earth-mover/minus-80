"""The ingest run end to end on the scaled-down plate from fixture.py.

uv run --package zebrafish-3d-4i-ingest pytest --import-mode=prepend ingests/zebrafish-3d-4i
"""

import json
from pathlib import Path

import fixture
import icechunk
import ingest
import numpy as np
import pytest
import verify
import zarr

from minus80.ome.verify import run_gates

#: Shallower than the real 32, so the fixture's 27 planes span full shards and a partial one.
SHARD_DEPTH = 8


@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    root = tmp_path_factory.mktemp("plate")
    truth = fixture.build(root)
    return fixture.zip_plate(root).as_uri(), truth


def _repo(path):
    return icechunk.Repository.open_or_create(icechunk.Storage.new_local_filesystem(str(path)))


def _convert(url, repo, **kw):
    return ingest.convert(
        url,
        repo,
        Path(url.removeprefix("file://")).with_suffix(".directory.parquet"),
        shard_depth=SHARD_DEPTH,
        **kw,
    )


@pytest.fixture(scope="module")
def store(plate, tmp_path_factory):
    url, _ = plate
    repo = _repo(tmp_path_factory.mktemp("repo"))
    assert _convert(url, repo) == list(fixture.WELLS)
    return zarr.open_group(repo.readonly_session("main").store, mode="r")


def test_every_gate_passes_on_every_plane(plate, store):
    url, _ = plate
    gates = verify.gates(verify.Reference(url), SHARD_DEPTH, sample=None)
    assert run_gates(store, gates, {"version": ingest.VERSION, "sample": 50}) == 0


def test_level_0_is_what_the_microscope_took(plate, store):
    """Channel ``4 * r + c`` of each embryo is channel ``c`` of round ``r``, as the fixture generated it."""
    _, truth = plate
    for well, (_, origins) in fixture.WELLS.items():
        for i in range(len(origins)):
            image = store[f"{well}/{i}"]
            assert image["0"].shape[0] == 4 * fixture.ROUNDS
            for rnd in range(fixture.ROUNDS):
                stored = image["0"][4 * rnd : 4 * rnd + 4]
                np.testing.assert_array_equal(stored, truth[(well, i, rnd)])
            for name in fixture.LABELS:
                stored = image[f"labels/{name}/0"][:]
                np.testing.assert_array_equal(stored, truth[(well, i, name)])


def test_channels_keep_their_source_names_and_rounds(store):
    channels = dict(store["B/02/0"].attrs)["ome"]["omero"]["channels"]
    expected = [name for names in fixture.CHANNELS for name in names]
    assert [c["label"] for c in channels] == expected
    notes = dict(store["B/02/0"].attrs)["annotations"]["channels"]
    assert [(c["round"], c["source_channel"]) for c in notes] == [
        (r, c) for r in range(fixture.ROUNDS) for c in range(4)
    ]


def test_the_scene_lays_the_well_out_as_the_source_did(store):
    for well, ((h, w), origins) in fixture.WELLS.items():
        ome = dict(store[well].attrs)["ome"]
        moves = {
            t["input"]["path"]: t["translation"] for t in ome["scene"]["coordinateTransformations"]
        }
        well_image = np.zeros((h, w), np.uint16)
        for path, (_, _, ty, tx) in moves.items():
            y, x = round(ty / fixture.PIXEL), round(tx / fixture.PIXEL)
            # Channel 4 is round 1's DAPI.
            well_image[y : y + fixture.F, x : x + fixture.F] = store[f"{well}/{path}/0"][4, 5]
        expected = np.zeros((h, w), np.uint16)
        for i, (y, x) in enumerate(origins):
            expected[y : y + fixture.F, x : x + fixture.F] = fixture.field_pixels(well, i, 1)[0, 5]
        np.testing.assert_array_equal(well_image, expected)


def test_every_round_is_recorded_including_round_3(store):
    rounds = dict(store.attrs)["source"]["rounds"]
    assert list(rounds) == [str(r) for r in range(fixture.ROUNDS)]
    assert rounds["3"][1] == "Nanog"
    assert dict(store.attrs)["source"]["source_acquisitions"] == list(range(fixture.LISTED_ROUNDS))
    images = dict(store["B/02"].attrs)["ome"]["well"]["images"]
    assert [i["path"] for i in images] == ["0", "1", "2"]


def test_a_field_on_the_far_edge_stops_where_the_source_level_does(store):
    # C/03 FOV_2 ends on the well image's last row, 90. Level 2 of a 90-row
    # image holds 22 rows, so the field's last coarse row was never stored.
    level = store["C/03/1/2"]
    assert level.shape[-2] == 22 - 50 // 4


def test_chunks_and_shards(store):
    image = store["B/02/0/0"]
    assert image.chunks == (1, 1, fixture.F, fixture.F)
    assert image.shards == (1, SHARD_DEPTH, fixture.F, fixture.F)
    label = store["B/02/0/labels/nucleiRaw/0"]
    assert label.chunks == (1, fixture.F, fixture.F)
    assert label.shards == (fixture.Z, fixture.F, fixture.F)


def test_a_stopped_run_resumes_and_the_plate_comes_last(plate, tmp_path):
    url, _ = plate
    repo = _repo(tmp_path / "repo")
    assert _convert(url, repo, wells=["B/02"]) == ["B/02"]
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    # Every well's group exists from the start; only a copied well has its metadata.
    assert ingest.committed_wells(root, list(fixture.WELLS)) == ["B/02"]
    assert "B/02" in [w["path"] for w in dict(root.attrs)["ome"]["plate"]["wells"]]
    assert _convert(url, repo, wells=["C/03"]) == ["C/03"]
    assert _convert(url, repo) == []
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert [w["path"] for w in dict(root.attrs)["ome"]["plate"]["wells"]] == list(fixture.WELLS)
    messages = [c.message for c in repo.ancestry(branch="main")]
    assert sum(m.startswith("Add well") for m in messages) == 2


def test_a_well_stopped_after_checkpoints_resumes_after_the_runs_they_hold(plate, tmp_path, monkeypatch, capsys):
    """Checkpoints commit the copied runs; a later run copies only the rest and ends with the same pixels."""
    url, _ = plate
    repo = _repo(tmp_path / "repo")
    check_well, stopped = ingest.check_well, []

    def stop_once(plan, *args):
        if not stopped:
            stopped.append(plan.well)
            raise ValueError("stopped after the copy")
        return check_well(plan, *args)

    monkeypatch.setattr(ingest, "check_well", stop_once)
    with pytest.raises(ingest.WellsFailed):
        _convert(url, repo, wells=["B/02"], checkpoint_seconds=0)
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert ingest.committed_wells(root, ["B/02"]) == []
    held = root["B/02"].attrs[ingest.PROGRESS]["complete_runs"]
    assert held

    decoded = []
    decode_run = ingest.decode_run
    monkeypatch.setattr(ingest, "decode_run", lambda run, *a: decoded.append(run.key) or decode_run(run, *a))
    assert _convert(url, repo, wells=["B/02"], checkpoint_seconds=0) == ["B/02"]
    assert "resuming B/02" in capsys.readouterr().out
    assert not {tuple(k) for k in held} & set(decoded), "runs the checkpoints held are not copied again"

    fresh = _repo(tmp_path / "fresh")
    _convert(url, fresh, wells=["B/02"])
    resumed = zarr.open_group(repo.readonly_session("main").store, mode="r")
    expected = zarr.open_group(fresh.readonly_session("main").store, mode="r")
    assert ingest.PROGRESS not in resumed["B/02"].attrs
    for path in ("B/02/0/0", "B/02/0/2", "B/02/0/labels/nucleiRaw/0"):
        np.testing.assert_array_equal(resumed[path][:], expected[path][:])


def test_verify_fails_a_store_missing_a_well(plate, tmp_path):
    url, _ = plate
    repo = _repo(tmp_path / "repo")
    _convert(url, repo, wells=["B/02"])
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    problems = verify.wells(root, verify.Reference(url))
    assert problems and "C/03" in problems[0]


def test_an_empty_source_chunk_is_listed_on_its_image(store):
    notes = dict(store["B/02/0"].attrs)["annotations"]
    assert fixture.EMPTY_CHUNK in notes["empty_source_chunks"]
    assert dict(store["B/02/1"].attrs)["annotations"]["empty_source_chunks"] == []


def test_a_truncated_source_chunk_reads_as_0_and_is_listed(store):
    notes = dict(store["C/03/0"].attrs)["annotations"]
    assert list(notes["unreadable_source_chunks"]) == [fixture.TRUNCATED_CHUNK]
    assert "truncated" in notes["unreadable_source_chunks"][fixture.TRUNCATED_CHUNK]
    assert dict(store["B/02/0"].attrs)["annotations"]["unreadable_source_chunks"] == {}


def test_each_well_is_read_once_in_file_order(plate, tmp_path, monkeypatch):
    """Every byte of a well's region is fetched exactly once, in order, in ranges of at most ``range_bytes``.

    Requests are counted where every range request in minus80 is made,
    ``minus80.core.fetch``; the central directory's own requests come from
    the end of the file and are left out.
    """
    import minus80.core.fetch as fetch
    from minus80.parsers.zip.reading import read_central_directory
    from minus80.parsers.zip.streaming import member_regions, spans_in_file_order

    url, _ = plate
    directory = read_central_directory(url)
    directory_offset = int(directory.schema.metadata[b"directory_offset"])
    requests = []
    fetched = fetch._get_async

    async def counted(store, key, where, start, end):
        requests.append((start, end))
        return await fetched(store, key, where, start, end)

    monkeypatch.setattr(fetch, "_get_async", counted)
    range_bytes = 64 << 10
    ingest.convert(
        url, _repo(tmp_path / "repo"), tmp_path / "directory.parquet",
        shard_depth=SHARD_DEPTH, range_bytes=range_bytes,
    )
    members = [r for r in requests if r[0] < directory_offset]
    names = directory["name"].to_pylist()
    wells = 0
    for well in fixture.WELLS:
        (region,) = member_regions(spans_in_file_order(directory, [n for n in names if n.startswith(f"{well}/")]))
        inside = [r for r in members if region[0] <= r[0] < region[1]]
        assert inside == sorted(inside), "requests leave in file order"
        assert inside[0][0] == region[0] and inside[-1][1] == region[1]
        assert all(a[1] == b[0] for a, b in zip(inside, inside[1:])), "each byte once, no gaps"
        assert all(end - start <= range_bytes for start, end in inside)
        assert len(inside) == -(-(region[1] - region[0]) // range_bytes)
        wells += len(inside)
    # The rest are the plate's own documents, one small request each.
    plate_documents = [n for n in names if n.count("/") <= 1]
    assert len(members) - wells == len(plate_documents)


def test_a_failed_well_is_reported_and_the_others_committed(plate, tmp_path, monkeypatch):
    url, _ = plate
    repo = _repo(tmp_path / "repo")
    check_well = ingest.check_well

    def fail_b02(plan, *args):
        if plan.well == "B/02":
            raise ValueError("a check failed")
        return check_well(plan, *args)

    monkeypatch.setattr(ingest, "check_well", fail_b02)
    with pytest.raises(ingest.WellsFailed) as failed:
        _convert(url, repo)
    assert list(failed.value.failed) == ["B/02"]
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert [w["path"] for w in dict(root.attrs)["ome"]["plate"]["wells"]] == ["C/03"]


def test_runs_waiting_past_the_limit_stop_the_well_naming_what_is_missing(plate, tmp_path):
    """The first runs wait for the field table; a limit below them stops each well."""
    url, _ = plate
    with pytest.raises(ingest.WellsFailed) as failed:
        _convert(url, _repo(tmp_path / "repo"), waiting_limit=1)
    assert list(failed.value.failed) == list(fixture.WELLS)
    assert all("still needs" in reason for reason in failed.value.failed.values())


def test_an_image_chunk_holding_several_channels_is_refused(plate):
    url, _ = plate
    from minus80.parsers.zip.reading import read_central_directory

    plan = ingest.WellPlan.from_directory(read_central_directory(url), "B/02")
    state = ingest.WellState(plan, {})
    document = json.dumps(
        {"zarr_format": 2, "shape": [4, 27, 90, 90], "chunks": [2, 10, 30, 30], "dtype": "<u2",
         "compressor": None, "filters": None, "fill_value": 0, "order": "C"}
    ).encode()
    with pytest.raises(ValueError, match="2 channels"):
        state.add_document("B/02/0/0/.zarray", document)
