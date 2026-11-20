"""The ingest run end to end on the scaled-down archive from fixture.py.

uv run --package chammi-75-ingest pytest --import-mode=prepend ingests/chammi-75
"""

import fixture
import icechunk
import ingest
import numpy as np
import pytest
import verify
import zarr


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    root = tmp_path_factory.mktemp("bucket")
    truth = fixture.build(root)
    return (root / "CHAMMI-75").as_uri() + "/", truth


@pytest.fixture(scope="module")
def built(archive, tmp_path_factory):
    url, _ = archive
    repo = str(tmp_path_factory.mktemp("repo"))
    work = tmp_path_factory.mktemp("work")
    ingest.run(repo, "main", url, work, fixture.STUDIES, parallel=2)
    return repo, work


@pytest.fixture(scope="module")
def store(built):
    repo, _ = built
    return verify.open_store(repo, "main")


@pytest.fixture(scope="module")
def index(store):
    return verify.load_index(store)


def test_verify_passes(archive, built):
    url, _ = archive
    repo, work = built
    failures = verify.verify(repo, "main", url, work, verify.CHECKS)
    assert failures == {check: [] for check in verify.CHECKS}


def test_an_excluded_or_unknown_study_is_refused():
    named = set(ingest.LICENSES) | set(ingest.EXCLUDED)
    with pytest.raises(ValueError, match="not allowlisted"):
        ingest.check_studies(named, ["nidr0013", "wtc0001"])
    with pytest.raises(ValueError, match="no recorded license decision"):
        ingest.check_studies(named | {"zzz0001"}, None)
    with pytest.raises(ValueError, match="does not name"):
        ingest.check_studies({"nidr0013"}, None)
    assert ingest.check_studies(named, None) == sorted(ingest.LICENSES)


def test_allowlist_is_the_59_studies_and_no_study_is_both_in_and_out():
    assert len(ingest.LICENSES) == 59 and len(ingest.EXCLUDED) == 15
    counts = {v: list(ingest.LICENSES.values()).count(v) for v in set(ingest.LICENSES.values())}
    assert counts == {ingest.CC_BY: 47, ingest.CC0: 9, ingest.CC_BY_SA_4: 2, ingest.CC_BY_SA_3: 1}
    assert not set(ingest.LICENSES) & set(ingest.EXCLUDED)


def test_store_holds_only_the_allowlisted_studies_with_their_licenses(store, index):
    assert sorted(set(store.group_keys()) - {"OME", "index", "cells"}) == fixture.STUDIES
    assert "wtc0001" not in set(index["study"])
    for study in fixture.STUDIES:
        assert dict(store[study].attrs)["annotations"]["license"] == ingest.LICENSES[study]
    assert dict(store.attrs)["licenses"]["excluded"]["wtc0001"] == ingest.NOT_PERMISSIVE
    assert "share_alike" in dict(store["idr0025"].attrs)["annotations"]
    assert "share_alike" not in dict(store["nidr0013"].attrs)["annotations"]
    assert "idr0025" in dict(store.attrs)["license"]


def test_every_plane_is_the_png_it_references(archive, store, index):
    _, truth = archive
    assert len(index) == sum(len(truth[i]["pixels"]) > 0 for i in truth if truth[i]["study"] in fixture.STUDIES)
    for r in index.itertuples():
        for j, slot in enumerate(r.channels.split("-")):
            stored = store[f"{r.stack}/ch{slot}/0"][r.row]
            np.testing.assert_array_equal(stored, truth[r.image_id]["pixels"][j])


def test_stacks_split_on_size_and_channel_order(index):
    stacks = sorted(index.loc[index["study"] == "idr0001", "stack"].unique())
    assert stacks == ["idr0001/24x20_ch1-2", "idr0001/48x36_ch1-2_nucleus_protein",
                      "idr0001/48x36_ch1-2_protein_nucleus"]


def test_channel_images_are_valid_ome_images_on_one_image_axis(store):
    stack = store["idr0001/24x20_ch1-2"]
    for slot, kind in ((1, "nucleus"), (2, "protein")):
        image = stack[f"ch{slot}"]
        multiscale = dict(image.attrs)["ome"]["multiscales"][0]
        axes = multiscale["coordinateSystems"][0]["axes"]
        assert [(a["name"], a["type"]) for a in axes] == [("image", "image"), ("y", "space"), ("x", "space")]
        assert image["0"].shape == (2, 20, 24) and image["0"].chunks == (1, 20, 24)
        assert dict(image.attrs)["annotations"]["channel_type"] == kind
    assert sorted(dict(store["OME"].attrs)["ome"]["series"])[:2] == ["idr0001/24x20_ch1-2/ch1",
                                                                     "idr0001/24x20_ch1-2/ch2"]


def test_sizes_come_from_the_png_and_fov_is_normalized(index):
    row = index.set_index("image_id").loc[fixture.WRONG_WIDTH]
    assert (row["width"], row["stated_width"]) == (24, "32")
    assert index.set_index("image_id").loc[fixture.TWO_FOV, "fov"] == "9"


def test_small_subset_is_matched_by_image_id(index):
    assert set(index.loc[index["in_small"], "image_id"]) == fixture.SMALL - {"wtc0001-0"}


def test_cells_follow_the_background_rule(archive, store, index):
    """Cellpose files lose entry 0 to cells/background; snake files are kept whole."""
    _, truth = archive
    cells = store["cells"]
    starts = cells["image_start"][:]
    assert starts[-1] == cells["position"].shape[0]
    for i, r in enumerate(index.itertuples()):
        source = truth[r.image_id]
        positions = cells["position"][starts[i]:starts[i + 1]]
        if source["entries"] is None:
            assert not r.has_positions and len(positions) == 0
            continue
        entries, sizes = source["entries"], source["sizes"]
        if r.study == "nidr0007":
            assert (r.segmentation, r.background_dropped) == ("snake", False)
            assert (cells["background"][i] == ingest.NO_BACKGROUND).all()
            np.testing.assert_array_equal(positions, entries)
        else:
            assert (r.segmentation, r.background_dropped) == ("cellpose", True)
            np.testing.assert_array_equal(cells["background"][i], entries[0])
            np.testing.assert_array_equal(positions, entries[1:])
        expected_area = sizes[1:] if sizes is not None else np.zeros(len(positions))
        np.testing.assert_array_equal(cells["area"][starts[i]:starts[i + 1]], expected_area)
        assert r.has_areas == (sizes is not None)


def test_a_study_already_in_the_store_is_refused(archive, built):
    url, _ = archive
    repo, work = built
    with pytest.raises(ValueError, match="already in the store"):
        ingest.run(repo, "main", url, work, ["nidr0013"], parallel=1)


def test_store_reads_in_a_fresh_repo_object(built):
    """The repo's config carries the container, so a reader needs no build-time setup."""
    repo, _ = built
    config = icechunk.Repository.open(icechunk.Storage.new_local_filesystem(repo)).config
    assert len(config.virtual_chunk_containers) == 1
    root = zarr.open_group(verify.open_store(repo, "main").store, mode="r")
    assert root["nidr0013/40x32_ch1/ch1/0"][0].dtype == np.uint8


def test_a_sample_holds_the_first_images_of_each_study_and_verifies(archive, tmp_path):
    url, _ = archive
    repo, work = str(tmp_path / "repo"), tmp_path / "work"
    ingest.run(repo, "main", url, work, fixture.STUDIES, parallel=1, sample=2)
    index = verify.load_index(verify.open_store(repo, "main"))
    for study in fixture.STUDIES:
        expected = sorted(i[0] for i in fixture.IMAGES[study])[:2]
        assert sorted(index.loc[index["study"] == study, "image_id"]) == expected
    failures = verify.verify(repo, "main", url, work, verify.CHECKS)
    assert failures == {check: [] for check in verify.CHECKS}
