"""The per-parent Datasets must agree with the standard NWB reader, not merely with h5py.

Byte-identity against `h5py` proves the references point at the right bytes. It does
not prove that what a neuroscientist gets out of the store (values in the right
units, on the right axis, for the right unit id) matches what they would get from
`pynwb`, the reader they actually use. These tests open the same NWB file both ways
and compare.
"""

from __future__ import annotations

import os
import warnings

import numpy as np
import pytest

pynwb = pytest.importorskip("pynwb")

from minus80.core import as_url, icechunk_io  # noqa: E402
from minus80.parsers.nwb import file_datatree  # noqa: E402


def _write(source, repo_path):
    """Write one file's tree and return a read-only store."""
    repo = icechunk_io.create_repo(str(repo_path), [as_url(source)])
    session = repo.writable_session("main")
    with warnings.catch_warnings():
        # A one-column table is only its own index coordinate, which the writer
        # flags as an entirely non-virtual Dataset.
        warnings.filterwarnings("ignore", message=".*entirely non-virtual Dataset.*")
        file_datatree(source).vz.to_icechunk(session.store)
    session.commit("test")
    return repo.readonly_session("main").store


@pytest.fixture(scope="module")
def source(nwb_files):
    return nwb_files[0]


@pytest.fixture(scope="module")
def store(source, tmp_path_factory):
    return _write(source, tmp_path_factory.mktemp("repo") / "r")


@pytest.fixture(scope="module")
def nwbfile(source):
    with pynwb.NWBHDF5IO(source, "r", load_namespaces=True) as io:
        yield io.read()


def _open(store, group):
    import xarray as xr

    return xr.open_zarr(store, group=group, consolidated=False, zarr_format=3)


def test_timeseries_values_and_axis(store, nwbfile):
    """A regularly-sampled series: same samples, and the same times for them.

    The axis is the interesting half. pynwb computes it from ``starting_time`` and
    ``rate``; the store wrote it out at ingest. They must agree, or ``.sel(time=…)``
    returns data from the wrong moment.
    """
    ref = nwbfile.acquisition["ElectricalSeries"]
    ds = _open(store, "acquisition/ElectricalSeries")

    assert np.array_equal(ds["data"].values, ref.data[:])
    expected = ref.starting_time + np.arange(ref.data.shape[0]) / ref.rate
    np.testing.assert_allclose(ds["time"].values, expected, rtol=0, atol=1e-9)
    assert ds["data"].attrs["unit"] == ref.unit
    assert ds["starting_time"].attrs["rate"] == ref.rate


def test_irregular_series_uses_its_own_timestamps(store, nwbfile):
    ref = nwbfile.processing["behavior"]["Position"]["SpatialSeriesLED1"]
    ds = _open(store, "processing/behavior/Position/SpatialSeriesLED1")

    assert np.array_equal(ds["data"].values, ref.data[:], equal_nan=True)
    assert np.array_equal(ds["time"].values, ref.timestamps[:])


def test_selecting_a_window_matches_pynwb(store, nwbfile):
    """`.sel(time=…)` picks the samples pynwb would pick for the same seconds."""
    ref = nwbfile.acquisition["ElectricalSeries"]
    ds = _open(store, "acquisition/ElectricalSeries")

    lo, hi = 1.0, 1.5
    got = ds["data"].sel(time=slice(lo, hi)).values
    i0 = int(np.ceil((lo - ref.starting_time) * ref.rate))
    i1 = int(np.floor((hi - ref.starting_time) * ref.rate))
    assert np.array_equal(got, ref.data[i0 : i1 + 1])


def test_unit_ids_index_the_row_axis(store, nwbfile):
    """A unit selected by id in the store is the same row pynwb calls that id."""
    ref = nwbfile.units
    ds = _open(store, "units")

    assert np.array_equal(ds["units"].values, ref.id[:])

    unit = int(ds["units"].values[0])
    row = list(ref.id[:]).index(unit)
    assert float(ds["depth"].sel(units=unit)) == pytest.approx(ref["depth"][row])


def test_electrode_table_ids(store, nwbfile):
    ds = _open(store, "general/extracellular_ephys/electrodes")
    assert np.array_equal(ds["electrodes"].values, nwbfile.electrodes.id[:])


def test_the_data_was_not_copied(source, tmp_path):
    """The arrays stay references; only the axes are real bytes.

    A store that quietly copied its data would pass every test above and miss the
    entire point. Measure the repo on disk rather than asking zarr: on a virtual array
    ``nbytes_stored()`` reports the size of the referenced source bytes, not what the
    repo holds.
    """
    _write(source, tmp_path / "r")

    on_disk = sum(
        os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(tmp_path / "r") for f in fs
    )
    assert on_disk < os.path.getsize(source) / 10
