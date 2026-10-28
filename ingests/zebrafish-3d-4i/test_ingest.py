"""The ingest run end to end on the scaled-down plate from fixture.py.

uv run --package zebrafish-3d-4i-ingest pytest --import-mode=prepend ingests/zebrafish-3d-4i
"""

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
        Path(url.removeprefix("file://")).with_suffix(".index.npz"),
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
        canvas = np.zeros((h, w), np.uint16)
        for path, (_, _, ty, tx) in moves.items():
            y, x = round(ty / fixture.PIXEL), round(tx / fixture.PIXEL)
            # Channel 4 is round 1's DAPI.
            canvas[y : y + fixture.F, x : x + fixture.F] = store[f"{well}/{path}/0"][4, 5]
        expected = np.zeros((h, w), np.uint16)
        for i, (y, x) in enumerate(origins):
            expected[y : y + fixture.F, x : x + fixture.F] = fixture.field_pixels(well, i, 1)[0, 5]
        np.testing.assert_array_equal(canvas, expected)


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
    assert "C/03" not in root
    assert "B/02" in [w["path"] for w in dict(root.attrs)["ome"]["plate"]["wells"]]
    assert _convert(url, repo, wells=["C/03"]) == ["C/03"]
    assert _convert(url, repo) == []
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert [w["path"] for w in dict(root.attrs)["ome"]["plate"]["wells"]] == list(fixture.WELLS)
    messages = [c.message for c in repo.ancestry(branch="main")]
    assert sum(m.startswith("Add well") for m in messages) == 2


def test_verify_fails_a_store_missing_a_well(plate, tmp_path):
    url, _ = plate
    repo = _repo(tmp_path / "repo")
    _convert(url, repo, wells=["B/02"])
    root = zarr.open_group(repo.readonly_session("main").store, mode="r")
    problems = verify.wells(root, verify.Reference(url))
    assert problems and "C/03" in problems[0]
