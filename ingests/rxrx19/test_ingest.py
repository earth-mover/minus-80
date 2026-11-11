"""The ingest run end to end on the small copy of gs://rxrx that fixture.py builds.

    uv run --package rxrx19-ingest pytest --import-mode=prepend ingests/rxrx19

The packed variant needs ice-pack: set ICE_PACK to its binary, or it is skipped.
"""

import os
import shutil

import fixture
import ingest
import verify
import numpy as np
import pytest
import zarr

import layout
from minus80.core.icechunk_io import open_repository


@pytest.fixture(scope="module")
def bucket(tmp_path_factory):
    root = tmp_path_factory.mktemp("bucket")
    return root, fixture.build(root)


def written(tmp_path, bucket, **options):
    root, truth = bucket
    repo_path = tmp_path / "repo"
    ingest.run(str(repo_path), "rxrx19a", source_root=root.as_uri(), cache=tmp_path / "cache", **options)
    repo = open_repository(repo_path)
    return zarr.open_group(repo.readonly_session("main").store, mode="r"), truth


def check_store(store, truth):
    pixels, rows = truth["pixels"], truth["rows"]
    for name, image in pixels.items():
        experiment, plate, rest = name.removeprefix("RxRx19a/images/").split("/")
        well, site, channel = rest.removesuffix(".png").split("_")
        path = layout.site_path("rxrx19a", experiment, int(plate.removeprefix("Plate")), well, int(site[1:]))
        assert np.array_equal(store[f"{path}/0"][int(channel[1:]) - 1], image), name

    blank = rows["HRCE-1_1_B03_1"]
    well = store[layout.well_path("rxrx19a", "HRCE-1", 1, "B03")].attrs.asdict()
    assert well["annotations"]["treatment"] is None and blank["treatment"] == ""
    assert [i["path"] for i in well["ome"]["well"]["images"]] == ["1", "2", "3", "4"]
    plate = store[layout.plate_path("rxrx19a", "HRCE-1", 1)].attrs.asdict()["ome"]["plate"]
    assert len(plate["rows"]) == 32 and len(plate["columns"]) == 48
    assert sorted(w["path"] for w in plate["wells"]) == ["AA/2", "B/2", "B/3"]
    site = store[layout.site_path("rxrx19a", "VERO-1", 1, "AB05", 3)]
    assert site.attrs["annotations"]["site_id"] == "VERO-1_1_AB05_3"
    assert site.attrs["ome"]["multiscales"][0]["name"] == "VERO-1_1_AB05_3"


def test_ingest_references_every_png_and_copies_the_embeddings(tmp_path, bucket):
    store, truth = written(tmp_path, bucket, embeddings=True)
    check_store(store, truth)
    site_ids = store["rxrx19a/site_id"][:]
    embeddings = store["rxrx19a/embeddings"][:]
    assert list(site_ids) == list(truth["rows"])
    for i, site_id in enumerate(site_ids):
        assert np.array_equal(embeddings[i], truth["embeddings"][site_id])


def test_some_plates_on_a_branch(tmp_path, bucket):
    root, truth = bucket
    repo_path = tmp_path / "repo"
    ingest.run(str(repo_path), "rxrx19a", plates="VERO-1/1", branch="trial",
               source_root=root.as_uri(), cache=tmp_path / "cache")
    repo = open_repository(repo_path)
    store = zarr.open_group(repo.readonly_session("trial").store, mode="r")
    assert list(store["rxrx19a"].group_keys()) == ["VERO-1"]
    with pytest.raises(zarr.errors.GroupNotFoundError):
        zarr.open_group(repo.readonly_session("main").store, mode="r")


@pytest.mark.skipif(not shutil.which(os.environ.get("ICE_PACK", "ice-pack")), reason="ice-pack not found")
def test_packed_references_read_the_same(tmp_path, bucket):
    store, truth = written(tmp_path, bucket, packed=True)
    check_store(store, truth)
    manifests = list((tmp_path / "repo" / "manifests").iterdir())
    assert len(manifests) == len(fixture.PLATES)


def test_verify_passes_on_the_written_store(tmp_path, bucket, capsys):
    root, _ = bucket
    written(tmp_path, bucket)
    code = verify.main(["--repo", str(tmp_path / "repo"), "--dataset", "rxrx19a", "--sites", "8",
                        "--source-root", root.as_uri(), "--cache", str(tmp_path / "cache")])
    assert code == 0, capsys.readouterr().out
