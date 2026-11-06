"""Small NWB files written with pynwb, so the suite needs no download.

Each file has the shapes the parser has to get right: a chunked, gzip-compressed
regularly-sampled ``ElectricalSeries`` whose channel axis is a
``DynamicTableRegion`` into the electrodes table, an irregularly-sampled
``SpatialSeries`` containing NaNs, a ``Units`` table with a ragged column, and
a trials table whose ``timeseries`` column is a compound dataset holding object
references. Ids are not ``0..n-1``, so a row position and a row id cannot be
mistaken for each other.

A generated file exercises pynwb's writer defaults, not every writer's (see
``docs/formats.md``). Set ``MINUS80_NWB_SAMPLES`` to two real NWB files,
separated by ``os.pathsep``, to run the same suite against them instead; the
tests assume only the groups named above.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import numpy as np
import pytest

pynwb = pytest.importorskip("pynwb")

N_SAMPLES = 400_000
N_CHANNELS = 4
RATE = 1250.0
STARTING_TIME = 0.5


def write_sample(path, seed: int) -> None:
    from hdmf.backends.hdf5.h5_utils import H5DataIO
    from pynwb.behavior import Position, SpatialSeries
    from pynwb.ecephys import ElectricalSeries

    rng = np.random.default_rng(seed)
    nwbfile = pynwb.NWBFile(
        session_description="synthetic ephys session",
        identifier=f"sample-{seed}",
        session_start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    device = nwbfile.create_device(name="probe")
    shank = nwbfile.create_electrode_group(
        name="shank0", description="one shank", location="CA1", device=device
    )
    for i in range(N_CHANNELS):
        nwbfile.add_electrode(
            id=100 + 7 * i, x=0.0, y=20.0 * i, z=0.0, location="CA1", group=shank
        )
    channels = nwbfile.create_electrode_table_region(
        region=list(range(N_CHANNELS)), description="all channels"
    )
    series = ElectricalSeries(
        name="ElectricalSeries",
        data=H5DataIO(
            rng.integers(-2000, 2000, (N_SAMPLES, N_CHANNELS), dtype="int16"),
            chunks=(10_000, N_CHANNELS),
            compression="gzip",
        ),
        electrodes=channels,
        rate=RATE,
        starting_time=STARTING_TIME,
        conversion=1e-6,
    )
    nwbfile.add_acquisition(series)
    nwbfile.add_trial(start_time=1.0, stop_time=2.0, timeseries=[series])
    nwbfile.add_trial(start_time=3.0, stop_time=4.5, timeseries=[series])

    n_pos = 3_000
    position = rng.normal(size=(n_pos, 2))
    position[rng.choice(n_pos, 12, replace=False)] = np.nan
    behavior = nwbfile.create_processing_module(name="behavior", description="tracking")
    behavior.add(
        Position(
            spatial_series=SpatialSeries(
                name="SpatialSeriesLED1",
                data=position,
                timestamps=np.cumsum(rng.uniform(0.01, 0.05, n_pos)),
                reference_frame="arena corner",
            )
        )
    )

    nwbfile.add_unit_column(name="depth", description="depth along the shank, um")
    for i in range(5):
        nwbfile.add_unit(
            id=900 + 3 * i,
            spike_times=np.sort(rng.uniform(0, N_SAMPLES / RATE, 50 + 10 * i)),
            depth=40.0 * i,
        )

    with pynwb.NWBHDF5IO(str(path), "w") as io:
        io.write(nwbfile)


@pytest.fixture(scope="session")
def nwb_files(tmp_path_factory):
    """Two sample files, different contents, written once per test session."""
    real = os.environ.get("MINUS80_NWB_SAMPLES")
    if real:
        return real.split(os.pathsep)
    root = tmp_path_factory.mktemp("nwb")
    paths = [root / "sub-01_ses-a_ecephys.nwb", root / "sub-01_ses-b_ecephys.nwb"]
    for seed, path in enumerate(paths):
        write_sample(path, seed)
    return [str(p) for p in paths]
