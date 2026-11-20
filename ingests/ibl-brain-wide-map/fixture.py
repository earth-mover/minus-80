"""A scaled-down IBL session: a raw NWB file and two spike sorts as ALF files.

The NWB file has what the real raw files have and the ingest must get right:
two probes, each with an AP and an LF ElectricalSeries in gzip chunks with a
timestamp per sample, and an electrodes table that lists probe01 first. The
sorts are ``.npy`` files with numpy's own header, whose spike columns do not
fill a whole number of chunks, so each has a short last chunk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np

CHANNELS = 8
SAMPLES = {"AP": 30_000, "LF": 2_500}
RATES = {"AP": 30_000.3, "LF": 2_500.03}
CONVERSION = {"AP": 2.34375e-06, "LF": 4.6875e-06}
SPIKES = {"2022": 1_000, "2024": 2_345}
CLUSTERS = {"2022": 7, "2024": 11}


def write_nwb(path: Path, eid: str) -> None:
    import pynwb
    from hdmf.backends.hdf5.h5_utils import H5DataIO
    from pynwb.ecephys import ElectricalSeries

    rng = np.random.default_rng(0)
    nwbfile = pynwb.NWBFile(session_description="scaled-down IBL session", identifier=eid,
                            session_id=eid, session_start_time=datetime(2020, 1, 28, tzinfo=timezone.utc))
    rows = {}
    for probe in ("Probe01", "Probe00"):
        device = nwbfile.create_device(name=probe)
        group = nwbfile.create_electrode_group(name=probe, description=probe, location="brain",
                                               device=device)
        rows[probe] = []
        for channel in range(CHANNELS):
            rows[probe].append(len(nwbfile.electrodes) if nwbfile.electrodes is not None else 0)
            nwbfile.add_electrode(x=float(channel), y=20.0 * channel, z=0.0,
                                  location=f"{probe} region", group=group)
    for probe in ("Probe00", "Probe01"):
        for band, n in SAMPLES.items():
            nwbfile.add_acquisition(ElectricalSeries(
                name=f"ElectricalSeries{probe}{band}",
                data=H5DataIO(rng.integers(-500, 500, (n, CHANNELS), dtype="int16"),
                              chunks=(1_000, CHANNELS), compression="gzip", compression_opts=4),
                timestamps=H5DataIO(np.arange(n) / RATES[band], chunks=(1_000,), compression="gzip"),
                electrodes=nwbfile.create_electrode_table_region(rows[probe], f"{probe} channels"),
                conversion=CONVERSION[band],
            ))
    with pynwb.NWBHDF5IO(str(path), "w") as io:
        io.write(nwbfile)


def write_sort(directory: Path, sort: str) -> dict[str, np.ndarray]:
    """One sort's ALF files; returns every array written, keyed ``<object>.<attribute>``."""
    rng = np.random.default_rng(int(sort))
    n, k = SPIKES[sort], CLUSTERS[sort]
    arrays = {
        "spikes.times": np.sort(rng.uniform(0, 1, n)),
        "spikes.clusters": rng.integers(0, k, n).astype("uint32"),
        "spikes.amps": rng.uniform(0, 1e-4, n),
        "clusters.depths": rng.uniform(0, 3840, k).astype("float32"),
        "clusters.waveforms": rng.normal(size=(k, 4, 3)).astype("float32"),
        "channels.localCoordinates": rng.uniform(0, 100, (CHANNELS, 2)).astype("float32"),
        "templates.amps": rng.uniform(0, 1, k),
    }
    directory.mkdir(parents=True, exist_ok=True)
    for name, values in arrays.items():
        np.save(directory / f"{name}.npy", values)
    uuids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(k)]
    (directory / "clusters.uuids.csv").write_text("uuids\n" + "\n".join(uuids) + "\n")
    return arrays
