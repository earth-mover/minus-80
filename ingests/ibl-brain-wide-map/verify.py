"""Spot-check the finished store against the sources, in about a minute.

What runs on every session and probe, with no request to the sources but one
``.npy`` header per spike column:

- completeness: both NWB files, and the four ALF objects of every probe;
- every probe's ``pykilosort`` group holds the sort each tag names, and
  ``main`` the latest;
- every array's codecs are ones zarr-python reads without minus80 or
  shadowzarr: bytes, zstd, vlen-utf8 and numcodecs.zlib;
- the spike columns' lengths equal what IBL's own listing implies from each
  file's size and header, so no file was cut short;
- metadata: every voltage array names its axes, carries a CF scale equal to
  NWB's conversion, and its series carries the SpikeGLX meta; the root says
  what the store is.

What runs on the first session and probe, reading the sources (about 30 MB):

- both NWB groups are NWBFiles of the session, with their spec cache;
- an AP window across a chunk boundary and the LF band's last samples equal
  h5py's decode of the NWB file;
- one spike-time chunk and the natively written last rows equal the ``.npy``
  bytes, at each tagged sort;
- xarray opens an ElectricalSeries in volts, with no sample masked as missing.

    uv run --package ibl-brain-wide-map-ingest python ingests/ibl-brain-wide-map/verify.py \\
        --repo <dir> [--sessions <eid>,...] [--probes probe00,probe01]
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import h5py
import numpy as np
import xarray as xr
import zarr
from ingest import NWB_FILES, OBJECTS, PROBES, SESSIONS, session_record, sort_files
from sources import dandi_file, npy_header, open_repo, say

from minus80.core.fetch import read_range
from minus80.core.remote import reader


#: The codecs a reader with only zarr-python installed can decode: Zarr's own
#: and numcodecs' zlib, which zarr-python depends on.
READABLE_CODECS = {"bytes", "zstd", "vlen-utf8", "numcodecs.zlib"}

#: Each pass's repo tag and the sort it holds, in the order the passes run.
SORT_TAGS = {"sort-2022": "2022", "sort-2024": "2024"}


def check(ok: bool, what: str, failures: list[str]) -> None:
    say(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failures.append(what)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sessions", default=",".join(SESSIONS))
    parser.add_argument("--probes", default=",".join(PROBES))
    args = parser.parse_args(argv)
    started = time.monotonic()
    failures: list[str] = []

    repo = open_repo(args.repo)
    tags = {t: repo.lookup_tag(t) for t in repo.list_tags()}
    store = repo.readonly_session("main").store
    root = zarr.open_group(store, mode="r")
    check(root.attrs.get("license") == "CC-BY-4.0" and "title" in root.attrs,
          "root names the dataset and its license", failures)

    codecs = {}
    for snapshot_store, subtree in [(store, None)] + [
            (repo.readonly_session(snapshot_id=snapshot).store, "alf") for snapshot in tags.values()]:
        for path, node in zarr.open_group(snapshot_store, mode="r").members(max_depth=None):
            if isinstance(node, zarr.Array) and (subtree is None or f"/{subtree}/" in path):
                chain = tuple(c["name"] for c in node.metadata.to_dict()["codecs"])
                codecs.setdefault(chain, path)
    check(all(name in READABLE_CODECS for chain in codecs for name in chain),
          f"every array's codecs are readable with zarr-python alone: {sorted(codecs)}", failures)

    probes = [(session_record(e), p) for e in args.sessions.split(",") for p in args.probes.split(",")]
    expected = {tag: sort for tag, sort in SORT_TAGS.items() if tag in tags}
    if expected:
        expected["main"] = list(expected.values())[-1]
    for ref, sort in expected.items():
        ref_store = store if ref == "main" else repo.readonly_session(tag=ref).store
        held = {f"{session['group']}/{probe}": zarr.open_group(
                    ref_store, path=f"{session['group']}/alf/{probe}/pykilosort", mode="r").attrs["sort"]
                for session, probe in probes}
        check(all(h == sort for h in held.values()),
              f"{ref}: every probe holds the {sort} sort ({len(held)} probes)", failures)
    for session, probe in probes:
        group = root[session["group"]]
        check(set(group.group_keys()) >= {*NWB_FILES, "alf"}, f"{session['group']}: NWB files and ALF",
              failures)
        for band in ("AP", "LF"):
            series = group[f"raw/acquisition/ElectricalSeries{probe.capitalize()}{band}"]
            data = series["data"]
            check(data.metadata.dimension_names == ("time", "channel")
                  and data.attrs["scale_factor"] == data.attrs["conversion"]
                  and series["timestamps"].shape == (data.shape[0],)
                  and series["electrodes"].shape == (data.shape[1],)
                  and "imSampRate" in series.attrs["spikeglx_meta"],
                  f"{series.path}: axes, scale, timestamps, electrodes and SpikeGLX meta", failures)
        sorting = group[f"alf/{probe}/pykilosort"]
        check(set(sorting.group_keys()) == set(OBJECTS), f"{sorting.path}: ALF objects", failures)
        columns = [(sorting[f"{obj}/{attribute}"], url, size)
                   for obj, attribute, url, size, _ in sort_files(session["eid"], probe, sorting.attrs["sort"])
                   if obj == "spikes"]
        with ThreadPoolExecutor(16) as pool:
            offsets = list(pool.map(lambda column: npy_header(column[1])[2], columns))
        for (array, url, size), offset in zip(columns, offsets):
            if array.shape[0] != (size - offset) // array.dtype.itemsize:
                check(False, f"{array.path}: {array.shape[0]} rows, file holds "
                      f"{(size - offset) // array.dtype.itemsize}", failures)

    session, probe = probes[0]
    for name in NWB_FILES:
        nwb = root[f"{session['group']}/{name}"]
        check(nwb.attrs["neurodata_type"] == "NWBFile" and nwb["general/session_id"][()] == session["eid"]
              and "specifications" in nwb,
              f"{nwb.path}: an NWBFile of the session, with its spec cache", failures)
    start = 13020 * 4000 - 100
    window = slice(start, start + 300)
    say(f"sources for {session['group']} {probe}")
    nwb = dandi_file(session["eid"], "raw")
    index = int(probe[-2:])
    base = f"{session['group']}/raw/acquisition/ElectricalSeriesProbe{index:02d}"
    with h5py.File(reader(nwb.url), "r") as f:
        ap_source = f[f"acquisition/ElectricalSeriesProbe{index:02d}AP/data"][window]
        lf_source = f[f"acquisition/ElectricalSeriesProbe{index:02d}LF/data"]
        tail = slice(lf_source.shape[0] - 500, lf_source.shape[0])
        lf_tail = lf_source[tail]
    check(np.array_equal(root[f"{base}AP/data"][window], ap_source),
          f"AP samples {window.start}-{window.stop} equal h5py's decode", failures)
    check(np.array_equal(root[f"{base}LF/data"][tail], lf_tail),
          "LF last 500 samples equal h5py's decode", failures)

    for tag, snapshot in sorted(tags.items()):
        sorting = zarr.open_group(repo.readonly_session(snapshot_id=snapshot).store, mode="r",
                                  path=f"{session['group']}/alf/{probe}/pykilosort")
        times = sorting["spikes/times"]
        url = times.attrs["source_url"]
        shape, dtype, offset = npy_header(url)
        rows = times.chunks[0]
        source = np.frombuffer(read_range(url, offset + rows * 8, offset + 2 * rows * 8), dtype=dtype)
        last = slice(shape[0] - 1000, shape[0])
        end = np.frombuffer(read_range(url, offset + last.start * 8, offset + shape[0] * 8), dtype=dtype)
        check(times.shape == shape and np.array_equal(times[rows:2 * rows], source)
              and np.array_equal(times[last], end),
              f"{tag} ({sorting.attrs['sort']}): {shape[0]:,} spike times; a chunk and the "
              "native last rows equal the file", failures)

    dataset = xr.open_zarr(store, group=f"{base}LF", consolidated=False)
    values = dataset["data"].isel(time=slice(0, 2000))
    raw = root[f"{base}LF/data"][:2000]
    check(values.dtype.kind == "f" and not bool(values.isnull().any())
          and np.allclose(values.values, raw * root[f"{base}LF/data"].attrs["conversion"]),
          "xarray opens the LF series in volts, with no sample masked as missing", failures)

    say(f"{len(failures)} failure(s) in {time.monotonic() - started:.0f} s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
