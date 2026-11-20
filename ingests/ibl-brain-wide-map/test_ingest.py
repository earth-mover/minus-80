"""The ingest run on the scaled-down session from fixture.py.

    uv run --package ibl-brain-wide-map-ingest pytest --import-mode=prepend ingests/ibl-brain-wide-map
"""

import csv
import datetime

import fixture
import ingest
import numpy as np
import pytest
import xarray as xr
import zarr
from hdmf_zarr import NWBZarrIO
from sources import DandiFile

from minus80.core.icechunk_io import commit, create_repo, open_repository
from minus80.core.registry import as_url

EID = "00000000-0000-0000-0000-000000000001"
#: Pinning the fixture's local files to a time after the test keeps them readable.
FUTURE = datetime.datetime(2100, 1, 1, tzinfo=datetime.timezone.utc)
SESSION = {"eid": EID, "group": f"sub-SUBJ-1/ses-{EID}", "session_path": "lab/Subjects/SUBJ_1/2020-01-28/001"}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("session")
    nwb_path = root / "raw.nwb"
    fixture.write_nwb(nwb_path, EID)
    sorts = {sort: fixture.write_sort(root / sort / "probe00", sort) for sort in fixture.SPIKES}

    def sort_files(eid, probe, sort):
        directory = root / sort / probe
        return [(*path.stem.split(".", 1), as_url(path), path.stat().st_size, "")
                for path in sorted(directory.glob("*.npy"))]

    def clusters_uuids(eid, probe, sort):
        with open(root / sort / probe / "clusters.uuids.csv") as f:
            return np.array([row["uuids"] for row in csv.DictReader(f)])

    patch = pytest.MonkeyPatch()
    patch.setattr(ingest, "sort_files", sort_files)
    patch.setattr(ingest, "clusters_uuids", clusters_uuids)
    patch.setattr(ingest, "SPIKE_ROWS_PER_CHUNK", 256)
    patch.setattr(ingest, "pinned_etag", lambda url, md5: FUTURE)

    repo_path = root / "repo"
    repo = create_repo(repo_path, [as_url(nwb_path), as_url(root / "2022" / "probe00" / "x"),
                                   as_url(root / "2024" / "probe00" / "x")])
    session = repo.writable_session("main")
    nwb = DandiFile(path="raw.nwb", url=str(nwb_path), https=str(nwb_path), size=0, sha256="")
    ingest.write_nwb_file(session, f"{SESSION['group']}/raw", nwb)
    ingest.write_sorting(session.store, SESSION, "probe00", "2022")
    tags = {"sort-2022": commit(session, "2022")}
    session = repo.writable_session("main")
    ingest.write_sorting(session.store, SESSION, "probe00", "2024")
    tags["sort-2024"] = commit(session, "2024")
    patch.undo()
    return open_repository(repo_path), tags, nwb_path, sorts


def test_raw_nwb_in_the_session_reads_with_zarr_as_the_file(built):
    import pynwb

    repo, _, nwb_path, _ = built
    raw = zarr.open_group(repo.readonly_session("main").store, path=f"{SESSION['group']}/raw", mode="r")
    assert raw.attrs["neurodata_type"] == "NWBFile"
    with pynwb.NWBHDF5IO(str(nwb_path), "r") as h5:
        for name, series in h5.read().acquisition.items():
            assert np.array_equal(raw[f"acquisition/{name}/data"][:], series.data[:]), name
            assert np.array_equal(raw[f"acquisition/{name}/timestamps"][:], series.timestamps[:]), name
            assert np.array_equal(raw[f"acquisition/{name}/electrodes"][:], series.electrodes.data[:])


def test_nwbzarrio_reads_the_raw_nwb_written_at_a_store_root(built, tmp_path):
    """NWBZarrIO opens only a store's root, so the convention is checked on a store of one file."""
    import pynwb

    _, _, nwb_path, _ = built
    repo = create_repo(tmp_path / "repo", [as_url(nwb_path)])
    session = repo.writable_session("main")
    ingest.write_nwb_file(session, "", DandiFile(path="raw.nwb", url=str(nwb_path),
                                                 https=str(nwb_path), size=0, sha256=""))
    commit(session, "one file at the root")
    store = open_repository(tmp_path / "repo").readonly_session("main").store
    with NWBZarrIO(store, mode="r") as io, pynwb.NWBHDF5IO(str(nwb_path), "r") as h5:
        got, want = io.read(), h5.read()
        for name, series in want.acquisition.items():
            assert np.array_equal(got.acquisition[name].data[:], series.data[:]), name
            assert list(got.acquisition[name].electrodes.data[:]) == list(series.electrodes.data[:])
        # The table lists probe01 first, so probe00's channels are rows CHANNELS onwards.
        assert got.acquisition["ElectricalSeriesProbe00AP"].electrodes.data[0] == fixture.CHANNELS


def test_voltages_open_in_xarray_as_volts(built):
    repo, _, _, _ = built
    store = repo.readonly_session("main").store
    group = f"{SESSION['group']}/raw/acquisition/ElectricalSeriesProbe00AP"
    dataset = xr.open_zarr(store, group=group, consolidated=False)
    raw = zarr.open_array(store, path=f"{group}/data", mode="r")[:]
    assert dataset["data"].dims == ("time", "channel")
    assert set(dataset.coords) == {"timestamps", "electrodes"}
    assert np.allclose(dataset["data"].values, raw * fixture.CONVERSION["AP"])


@pytest.mark.parametrize("sort", sorted(fixture.SPIKES))
def test_each_tag_holds_its_own_sort(built, sort):
    repo, tags, _, sorts = built
    store = repo.readonly_session(snapshot_id=tags[f"sort-{sort}"]).store
    group = zarr.open_group(store, path=f"{SESSION['group']}/alf/probe00/pykilosort", mode="r")
    assert group.attrs["sort"] == sort
    for name, values in sorts[sort].items():
        obj, attribute = name.split(".", 1)
        array = group[f"{obj}/{attribute}"]
        assert array.shape == values.shape and np.array_equal(array[:], values), name
    assert group["spikes/times"].shape[0] % 256 != 0
    assert group["clusters/uuids"].shape == (fixture.CLUSTERS[sort],)


class Stop(Exception):
    """Stands in for a job killed partway through a pass."""


@pytest.fixture
def relaunchable(tmp_path, monkeypatch):
    """Two sessions whose ingest can be stopped after a given number of session commits."""
    root = tmp_path / "sources"
    root.mkdir()
    nwb_path = root / "raw.nwb"
    fixture.write_nwb(nwb_path, EID)
    for sort in fixture.SPIKES:
        fixture.write_sort(root / sort / "probe00", sort)
    sessions = {f"eid-{i}": {"eid": f"eid-{i}", "group": f"sub-SUBJ-{i}/ses-eid-{i}",
                             "session_path": f"lab/Subjects/SUBJ_{i}/2020-01-28/001",
                             "lab": "lab", "subject": f"SUBJ_{i}", "date": "2020-01-28",
                             "number": "001", "task_protocol": "fixture"} for i in (1, 2)}
    nwb = DandiFile(path="raw.nwb", url=str(nwb_path), https=str(nwb_path), size=0, sha256="")
    monkeypatch.setattr(ingest, "session_record", lambda eid: sessions[eid])
    monkeypatch.setattr(ingest, "dandi_file", lambda eid, name: nwb)
    monkeypatch.setattr(ingest, "spikeglx_meta", lambda session, probe, band: {"imSampRate": "30000"})
    monkeypatch.setattr(ingest, "sort_files", lambda eid, probe, sort: [
        (*path.stem.split(".", 1), as_url(path), path.stat().st_size, "")
        for path in sorted((root / sort / probe).glob("*.npy"))])
    monkeypatch.setattr(ingest, "clusters_uuids", lambda eid, probe, sort: np.array(
        [f"u{i}" for i in range(fixture.CLUSTERS[sort])]))
    monkeypatch.setattr(ingest, "SPIKE_ROWS_PER_CHUNK", 256)
    monkeypatch.setattr(ingest, "pinned_etag", lambda url, md5: FUTURE)
    repo = create_repo(tmp_path / "repo", [], prefixes=[root.as_uri() + "/"])
    write_session = ingest.write_session

    def launch(stop_after=None):
        """Both passes as run.sh runs them, stopped after ``stop_after`` session commits."""
        written = []

        def counted(*args, **kwargs):
            if stop_after is not None and len(written) == stop_after:
                raise Stop
            written.append(write_session(*args, **kwargs))

        monkeypatch.setattr(ingest, "write_session", counted)
        try:
            ingest.run_pass(repo, list(sessions), ["probe00"], "2022", False, "sort-2022")
            ingest.run_pass(repo, list(sessions), ["probe00"], "2024", True, "sort-2024")
        finally:
            monkeypatch.setattr(ingest, "write_session", write_session)
        return written

    return repo, list(sessions), launch


def sorts_at(repo, eids, **ref):
    store = repo.readonly_session(**ref).store
    return [zarr.open_group(store, path=f"sub-SUBJ-{eid[-1]}/ses-{eid}/alf/probe00/pykilosort",
                            mode="r").attrs["sort"] for eid in eids]


@pytest.mark.parametrize("stop_after", [1, 2, 3], ids=[
    "inside the 2022 pass", "after sort-2022", "inside the 2024 pass"])
def test_a_relaunch_resumes_without_rewriting_or_reverting(relaunchable, stop_after):
    repo, eids, launch = relaunchable
    with pytest.raises(Stop):
        launch(stop_after=stop_after)
    if stop_after >= 2:
        assert "sort-2022" in repo.list_tags()
    launch()
    assert sorts_at(repo, eids, tag="sort-2022") == ["2022", "2022"]
    assert sorts_at(repo, eids, tag="sort-2024") == ["2024", "2024"]
    assert sorts_at(repo, eids, branch="main") == ["2024", "2024"]
    messages = [c.message for c in repo.ancestry(branch="main")]
    assert sum("NWB files" in m for m in messages) == 2
    assert sum("the 2024 spike sorting" in m for m in messages) == 2
    assert launch() == []
