"""The GTEx ingest run end to end on fixture.py's two slides, served over HTTP.

uv run --package idc-ingest pytest --import-mode=prepend ingests/idc
"""

import fixture
import icechunk
import ingest
import numpy as np
import pytest
import verify
import zarr
from minus80.core.icechunk_io import open_repository
from plumbing import Source

FACTS = {"idc_version": "v25", "idc_index_data": "25.0.5", "citations": ["A fixture."]}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("bucket")
    truth = fixture.build(root)
    with fixture.serve(root) as (url, listing, tags):
        source = Source(root=url, listing=listing)
        location = str(tmp_path_factory.mktemp("repo") / "repo")
        ingest.run(location, "gtex", fixture.catalog(truth), source, FACTS, workers=2)
        repo = open_repository(location)
        yield truth, repo, source, tags, location


@pytest.fixture
def store(built):
    _, repo, _, _, _ = built
    return zarr.open_group(repo.readonly_session("main").store, mode="r")


def test_verify_passes(built, store):
    truth, _, source, _, _ = built
    failures, stored = verify.check_store(store, set(truth), partial=False)
    assert failures == []
    assert set(stored) == set(truth)
    import random
    rng = random.Random(0)
    for path in stored.values():
        assert verify.check_pixels(store, path, source, rng) == []


def test_every_level_reads_as_the_tiles_decode(built, store):
    truth, _, _, _, _ = built
    for slide, t in truth.items():
        group = store[f"gtex/{t['patient']}/{slide}"]
        for k, expected in enumerate(t["levels"]):
            np.testing.assert_array_equal(group[f"image/{k}"][:], expected)
        np.testing.assert_array_equal(group["thumbnail/0"][:], t["thumbnail"])
        np.testing.assert_array_equal(group["overview/0"][:], t["overview"])


def test_scale_is_the_stated_spacing_in_micrometers(built, store):
    truth, _, _, _, _ = built
    slide, t = next(iter(truth.items()))
    multiscale = store[f"gtex/{t['patient']}/{slide}/image"].attrs["ome"]["multiscales"][0]
    scale = multiscale["datasets"][0]["coordinateTransformations"][0]["transformations"][0]["scale"]
    assert scale == pytest.approx([1.0, fixture.SPACING_MM * 1000, fixture.SPACING_MM * 1000])


def test_headers_travel_with_the_slide(built, store):
    import json

    truth, _, _, _, _ = built
    slide, t = next(iter(truth.items()))
    headers = json.loads(bytes(store[f"gtex/{t['patient']}/{slide}/dicom"][:]))
    assert sorted(headers) == ["image/0", "image/1", "overview", "thumbnail"]
    assert headers["image/0"]["00280008"]["Value"] == [9]  # 600 x 700 in 240-pixel tiles: 3 x 3


def test_root_names_every_codec_and_the_release(store):
    assert store.attrs["idc_version"] == "v25"
    assert set(store.attrs["codecs"]) >= {"imagecodecs_jpeg", "transpose", "bytes"}
    assert store.attrs["left_out"] == {}


def test_a_rerun_skips_written_slides(built, capsys):
    truth, repo, source, _, location = built
    before = [s.id for s in repo.ancestry(branch="main")]
    ingest.run(location, "gtex", fixture.catalog(truth), source, FACTS, workers=2)
    assert "0 to write" in capsys.readouterr().out
    after = [s.id for s in open_repository(location).ancestry(branch="main")]
    assert after == before


def test_references_are_pinned_to_the_etag(built, store):
    """A source rewritten in place is refused rather than read at stale offsets."""
    truth, repo, _, tags, _ = built
    slide, t = next(iter(truth.items()))
    path = f"/{t['series']}/level-1.dcm"
    group = f"gtex/{t['patient']}/{slide}/image/1"
    store[group][:]
    original = tags[path]
    tags[path] = '"rewritten"'
    try:
        with pytest.raises(icechunk.IcechunkError, match="checksum"):
            zarr.open_group(repo.readonly_session("main").store, mode="r")[group][:]
    finally:
        tags[path] = original
