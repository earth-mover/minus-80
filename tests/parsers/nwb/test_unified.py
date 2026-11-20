"""An NWB file written into Icechunk in the unified Zarr v3 convention reads back as NWB and as xarray."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
import zarr

pytest.importorskip("shadowzarr")
hdmf_zarr = pytest.importorskip("hdmf_zarr")

from minus80.core.icechunk_io import commit, create_repo, open_repository  # noqa: E402
from minus80.core.registry import as_url  # noqa: E402
from minus80.parsers.nwb.unified import (  # noqa: E402
    name_dimensions,
    nwb_references,
    under_group,
    write_nwb,
)

GROUP = "subject/2024-01-01/001/raw"


def written(source, repo_path, group):
    repo = create_repo(repo_path, [as_url(source)])
    session = repo.writable_session("main")
    write_nwb(name_dimensions(under_group(nwb_references(source), group)), session,
              url_for=lambda path: as_url(path))
    commit(session, "one NWB file")
    return open_repository(repo_path).readonly_session("main").store


@pytest.fixture(scope="module")
def store(nwb_files, tmp_path_factory):
    """The file under a session group of a larger store."""
    return written(nwb_files[0], tmp_path_factory.mktemp("grouped") / "repo", GROUP), nwb_files[0]


def test_nwbzarrio_reads_a_store_holding_the_file_as_the_hdf5_file(nwb_files, tmp_path):
    source = nwb_files[0]
    zarr_store = written(source, tmp_path / "repo", "")
    with pynwb_file(source) as expected, hdmf_zarr.NWBZarrIO(zarr_store, mode="r") as io:
        got = io.read()
        want = expected.acquisition["ElectricalSeries"]
        series = got.acquisition["ElectricalSeries"]
        assert np.array_equal(series.data[:], want.data[:])
        assert series.rate == want.rate and series.conversion == want.conversion
        assert list(series.electrodes.data[:]) == list(want.electrodes.data[:])
        assert list(got.units.id[:]) == list(expected.units.id[:])


def test_the_grouped_file_reads_with_zarr_alone(store):
    zarr_store, source = store
    import pynwb

    group = zarr.open_group(zarr_store, path=GROUP, mode="r")
    assert group.attrs["neurodata_type"] == "NWBFile"
    with pynwb.NWBHDF5IO(source, "r") as io:
        want = io.read().acquisition["ElectricalSeries"].data[:]
    assert np.array_equal(group["acquisition/ElectricalSeries/data"][:], want)


def test_every_array_has_dimension_names_and_series_line_up(store):
    zarr_store, _ = store
    group = zarr.open_group(zarr_store, path=GROUP, mode="r")
    for path, node in group.members(max_depth=None):
        if isinstance(node, zarr.Array):
            assert node.metadata.dimension_names is not None, path
    series = xr.open_zarr(zarr_store, group=f"{GROUP}/acquisition/ElectricalSeries", consolidated=False)
    assert series["data"].dims == ("time", "channel")
    assert series["electrodes"].dims == ("channel",)
    electrodes = xr.open_zarr(zarr_store, group=f"{GROUP}/general/extracellular_ephys/electrodes",
                              consolidated=False)
    assert set(electrodes.sizes) == {"electrodes"}


def pynwb_file(path):
    import contextlib

    import pynwb

    @contextlib.contextmanager
    def opened():
        with pynwb.NWBHDF5IO(path, "r") as io:
            yield io.read()

    return opened()


def test_a_contiguous_dataset_moves_under_the_group_with_its_chunks(tmp_path):
    """shadowzarr describes a contiguous dataset's chunks with a gen entry, whose key moves too."""
    import h5py

    source = str(tmp_path / "contiguous.h5")
    values = np.arange(1_500_000, dtype="<f8")  # 12 MB, cut into 4 MiB chunks by shadowzarr
    with h5py.File(source, "w") as f:
        f.create_dataset("events/timestamps", data=values)
    rfs = nwb_references(str(source))
    assert rfs.get("gen"), "the fixture should exercise a gen entry"
    zarr_store = written(source, tmp_path / "repo", "sub-1/ses-1/raw")
    array = zarr.open_array(zarr_store, path="sub-1/ses-1/raw/events/timestamps", mode="r")
    assert np.array_equal(array[:], values)
