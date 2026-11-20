"""The ingest run end to end on the small scan from fixture.py, both exports.

    uv run --package microns-ingest pytest ingests/microns
"""

import random

import fixture
import h5py
import ingest
import numpy as np
import pytest
import verify
import zarr

from minus80.core import as_url
from minus80.core.icechunk_io import create_repo
from minus80.ome.verify import GATES, run_gates


@pytest.fixture(scope="module")
def scan(tmp_path_factory):
    root = tmp_path_factory.mktemp("source")
    truth = fixture.build(root)
    found = ingest.Scan(fixture.ANIMAL, fixture.SESSION, fixture.SCAN,
                        tiffs={v: as_url(p) for v, p in truth.tiffs.items()},
                        nwb=as_url(truth.nwb), nwb_path=truth.nwb.name)
    return found, truth, as_url(root) + "/"


def commit_both(repo, facts):
    ingest.commit_export(repo, [facts], 1, parallel=1)
    first = ingest.committed_exports(repo)[1]
    ingest.commit_export(repo, [facts], 2, parallel=1, earlier=ingest.sample_pages(first))


@pytest.fixture(scope="module")
def repo(scan, tmp_path_factory):
    found, _, prefix = scan
    repo = create_repo(tmp_path_factory.mktemp("repo"), [], prefixes=[prefix])
    commit_both(repo, ingest.read_nwb(found))
    assert set(repo.list_tags()) == set()
    ingest.tag_exports(repo)
    return repo


@pytest.fixture(scope="module")
def roots(repo):
    return {tag: zarr.open_group(repo.readonly_session(tag=tag).store, mode="r") for tag in verify.TAGS}


def field(root, n):
    return root[f"{ingest.SCANS}/{fixture.SESSION}-{fixture.SCAN}/field-{n}"]


def test_every_frame_of_every_field_is_its_interleaved_page(scan, roots):
    _, truth, _ = scan
    for n in range(1, fixture.FIELDS + 1):
        want = truth.pages[n - 1::fixture.FIELDS]
        level = field(roots["v2"], n)["0"]
        assert level.shape == (fixture.VOLUMES, 1, fixture.ROWS, fixture.COLUMNS)
        assert level.dtype == np.int16
        np.testing.assert_array_equal(level[:, 0], want)
        first = field(roots["v1"], n)["0"]
        assert first.dtype == np.uint16
        np.testing.assert_array_equal(first[:, 0], want.view(np.uint16))


def test_each_export_references_its_own_file(scan, roots):
    found, _, _ = scan
    for tag, export in (("v1", 1), ("v2", 2)):
        attrs = dict(field(roots[tag], 1).attrs)
        assert attrs["annotations"]["source_url"] == found.tiffs[export]
        assert dict(field(roots[tag], 1)["0"].attrs)["source_url"] == found.tiffs[export]
        assert attrs["annotations"]["tiff_export"]["export"] == tag


def test_fields_are_placed_by_their_nwb_centers(roots):
    group = roots["v2"][f"{ingest.SCANS}/{fixture.SESSION}-{fixture.SCAN}"]
    transforms = {t["input"]["path"]: t["translation"]
                  for t in dict(group.attrs)["ome"]["scene"]["coordinateTransformations"]}
    pixel = fixture.PIXEL_M * 1e6
    for n, (x, y, depth) in enumerate(fixture.CENTERS_M, 1):
        t, z, top, left = transforms[f"field-{n}"]
        assert z == pytest.approx(depth * 1e6)
        assert top + (fixture.ROWS - 1) / 2 * pixel == pytest.approx(y * 1e6)
        assert left + (fixture.COLUMNS - 1) / 2 * pixel == pytest.approx(x * 1e6)


def test_volume_times_and_time_scale_come_from_the_nwb(scan, roots):
    _, truth, _ = scan
    group = roots["v2"][f"{ingest.SCANS}/{fixture.SESSION}-{fixture.SCAN}"]
    np.testing.assert_array_equal(group["volume_times"][:], truth.times)
    (multiscale,) = dict(field(roots["v2"], 1).attrs)["ome"]["multiscales"]
    scale = multiscale["datasets"][0]["coordinateTransformations"][0]["scale"]
    assert scale == pytest.approx([1 / fixture.RATE_HZ, ingest.PLANE_UM,
                                   fixture.PIXEL_M * 1e6, fixture.PIXEL_M * 1e6])


def test_the_shared_gates_pass_through_both_tags(roots):
    gates = {k: GATES[k] for k in ("has images", "schema", "dimension names", "physical size",
                                   "series matches", "provenance")}
    for root in roots.values():
        assert run_gates(root, gates, {"version": ingest.VERSION, "sample": 1}) == 0


def test_verify_passes(scan, roots):
    found, truth, _ = scan
    name = f"{fixture.SESSION}-{fixture.SCAN}"
    for root in roots.values():
        assert verify.complete(root, [name]) == []
    with h5py.File(truth.nwb, "r") as nwb:
        assert verify.times(roots["v2"], name, nwb) == []
        assert verify.pixels(roots, name, nwb, rng=random.Random(0)) == []


def test_tags_and_messages_name_each_export(repo):
    assert set(repo.list_tags()) == {"v1", "v2"}
    messages = [s.message for s in repo.ancestry(tag="v2")]
    assert "v2 TIFF export" in messages[0] and "same pixel bytes" in messages[0]
    assert "v1 TIFF export" in messages[1]


def test_a_tiff_that_disagrees_with_the_nwb_is_refused(scan, tmp_path):
    found, truth, _ = scan
    facts = ingest.read_nwb(found)
    short = tmp_path / "short.tif"
    import tifffile
    tifffile.imwrite(short, truth.pages[:-1], bigtiff=True, photometric="minisblack")
    with pytest.raises(ValueError, match="the NWB file states"):
        ingest.field_arrays(as_url(short), facts, 2)


def test_exports_with_different_bytes_are_refused(scan, tmp_path):
    found, truth, _ = scan
    other = tmp_path / "other.tif"
    import tifffile
    tifffile.imwrite(other, truth.pages + 1, bigtiff=True, photometric="minisblack")
    facts = ingest.read_nwb(found)
    arrays, _ = ingest.field_arrays(as_url(other), facts, 2)
    pages = fixture.VOLUMES * fixture.FIELDS
    first, _ = ingest.field_arrays(found.tiffs[1], facts, 1)
    with pytest.raises(ValueError, match="different bytes"):
        ingest.compare_pages(found.tiffs[1], ingest.sample_offsets(first, facts, pages),
                             as_url(other), ingest.sample_offsets(arrays, facts, pages), arrays)


def test_a_tiff_of_the_wrong_dtype_for_its_export_is_refused(scan):
    found, _, _ = scan
    with pytest.raises(ValueError, match="declares uint16"):
        ingest.field_arrays(found.tiffs[2], ingest.read_nwb(found), 1)


def test_a_rerun_writes_only_what_is_not_committed(scan, tmp_path):
    found, _, prefix = scan
    facts = ingest.read_nwb(found)
    repo = create_repo(tmp_path / "repo", [], prefixes=[prefix])
    first = ingest.commit_export(repo, [facts], 1, parallel=1)
    # The run stops here; a rerun finds the first export and writes the second alone.
    assert ingest.commit_export(repo, [facts], 1, parallel=1) == first
    commit_both(repo, facts)
    commits = [s.id for s in repo.ancestry(branch="main")]
    commit_both(repo, facts)
    assert [s.id for s in repo.ancestry(branch="main")] == commits
    assert len(commits) == 3  # the repo's initial commit and one per export
    assert ingest.tag_exports(repo) == ingest.tag_exports(repo)


def test_an_export_committed_without_a_scan_is_refused(scan, tmp_path):
    found, _, prefix = scan
    facts = ingest.read_nwb(found)
    repo = create_repo(tmp_path / "repo", [], prefixes=[prefix])
    ingest.commit_export(repo, [facts], 1, parallel=1)
    other = ingest.ScanFacts(scan=ingest.Scan(found.animal, 9, 9, found.tiffs, found.nwb, found.nwb_path),
                             rate_hz=facts.rate_hz, volume_times=facts.volume_times, fields=facts.fields)
    with pytest.raises(ValueError, match="without scans"):
        ingest.commit_export(repo, [facts, other], 1, parallel=1)
