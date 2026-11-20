"""Reference the IBL Brain-Wide Map's NWB files and spike sorting in one Icechunk store.

The International Brain Laboratory recorded 459 sessions of mice doing a
visual decision task, with one or two Neuropixels probes per session. Two
hosts publish the results:

- DANDI 000409 holds two NWB files per session: the raw one, with both
  probes' AP band (30 kHz, spikes) and LF band (2.5 kHz, local field
  potential) in 10 MB gzip chunks, and the processed one, with the trials,
  behavior and the current spike sorting.
- The IBL bucket holds everything as ALF files (one ``.npy`` per attribute),
  including each spike sorting IBL has published. A re-sort goes in a dated
  revision folder beside the original, so the 2022 sort and the 2024 re-sort
  sit side by side.

The store references both without copying them. Each session is a group
``sub-<subject>/ses-<eid>``, named as on DANDI, holding:

- ``raw`` and ``processed``: the two NWB files, each written whole in the
  unified NWB Zarr v3 convention of hdmf-zarr 0.14, exactly as shadowzarr
  maps it. Every array also has ``dimension_names``, so a TimeSeries or table
  group opens with ``xr.open_zarr``, and each voltage array carries CF
  ``scale_factor`` and ``units``.
- ``alf/<probe>/pykilosort``: the ALF objects ``spikes``, ``clusters``,
  ``channels`` and ``templates`` of one sort, each an xarray-style group whose
  arrays share the object's first axis.

The spike sorting is written once per published revision, each as its own
commit with its own tag, so a reader can check out either sort.

Running it
----------
::

    uv run --package ibl-brain-wide-map-ingest python ingests/ibl-brain-wide-map/ingest.py \\
        --repo <dir> --sort 2022 [--sessions <eid>,...] [--probes probe00] [--tag sort-2022]
    uv run --package ibl-brain-wide-map-ingest python ingests/ibl-brain-wide-map/ingest.py \\
        --repo <dir> --sort 2024 --sorting-only [--sessions <eid>,...] [--tag sort-2024]

PLAN.md explains the layout; LAYOUTS.md records the sessions chosen and what
the source taught.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import time

import numpy as np
import zarr
from sources import (
    DandiFile,
    bucket_url,
    dandi_file,
    npy_header,
    one_cache,
    open_repo,
    pinned_etag,
    say,
)
from virtualizarr.manifests import ManifestArray, ManifestGroup

from minus80.core.builders.flat_binary import flat_manifest_array, flat_tail
from minus80.core.fetch import read_range
from minus80.core.icechunk_io import commit, pin_virtual_refs
from minus80.core.registry import as_url
from minus80.parsers.nwb.unified import name_dimensions, nwb_references, under_group, write_nwb

# The sessions of phase 1. Every one is in both the 2022 release and the 2024
# re-sort with two probes, and has its raw NWB on DANDI. Together they cover
# ten labs and all ten of the Cosmos atlas's top-level regions. LAYOUTS.md has
# the selection and the alternatives.
SESSIONS = [
    "465c44bd-2e67-4112-977b-36e1ac7e3f8c",  # mainenlab ZM_2241 2020-01-28
    "eacc49a9-f3a1-49f1-b87f-0972f90ee837",  # steinmetzlab NR_0020 2022-05-09
    "1191f865-b10a-45c8-9c48-24a980fd9402",  # churchlandlab CSHL060 2020-03-11
    "4ecb5d24-f5cc-402c-be28-9d0f7cb14b3a",  # hoferlab SWC_043 2020-09-21
    "5339812f-8b91-40ba-9d8f-a559563cc46b",  # danlab DY_016 2020-09-11
    "aec5d3cc-4bb2-4349-80a9-0395b76f04e2",  # angelakilab NYU-37 2021-01-30
    "56b57c38-2699-4091-90a8-aba35103155e",  # mrsicflogellab SWC_054 2020-10-05
    "2bdf206a-820f-402f-920a-9e86cd5388a4",  # churchlandlab_ucla UCLA037 2022-02-02
    "6274dda8-3a59-4aa1-95f8-a8a549c46a26",  # zadorlab CSH_ZAD_025 2020-07-30
    "09394481-8dd2-4d5c-9327-f2753ede92d7",  # wittenlab ibl_witten_26 2021-01-30
]
PROBES = ("probe00", "probe01")

# Each published sort: the ONE cache that lists it and the revision folder
# its files sit in. The 2022 sort is the default revision of the original
# release; the re-sort was published in a revision folder dated 2024-05-06
# (the release notes date the re-run 2024-05-15).
SORTS = {
    "2022": {"cache": "2022_Q4_IBL_et_al_BWM", "revision": ""},
    "2024": {"cache": "Brainwidemap", "revision": "#2024-05-06#/"},
}


# Sessions
# --------
# IBL names a session by a UUID (its "eid") and by its folder,
# <lab>/Subjects/<subject>/<date>/<number>. DANDI files the same session as
# sub-<subject>/sub-<subject>_ses-<eid>_..., the BIDS-style naming that DANDI,
# BIDS and the NWB tools share. The store names each session group after
# DANDI, sub-<subject>/ses-<eid>, because the NWB files it holds are DANDI's
# and a reader can find both in the dandiset by the same names. IBL's folder,
# lab, date and number are in the session's attributes.


def session_record(eid: str) -> dict:
    sessions = one_cache(SORTS["2024"]["cache"])["sessions"]
    row = sessions.loc[eid]
    number = f"{int(row['number']):03d}"
    subject = dandi_file(eid, "raw").path.split("/", 1)[0]
    return {
        "eid": eid,
        "lab": row["lab"],
        "subject": row["subject"],
        "date": str(row["date"]),
        "number": number,
        "task_protocol": row["task_protocol"],
        "session_path": f"{row['lab']}/Subjects/{row['subject']}/{row['date']}/{number}",
        "group": f"{subject}/ses-{eid}",
    }


# The NWB files
# -------------
# DANDI's two files per session are written whole, in the unified NWB Zarr v3
# convention hdmf-zarr 0.14 reads: shadowzarr reads each file's headers and
# returns its tree as Zarr metadata, chunk references and small datasets
# inline. The raw file's per-probe ElectricalSeries hold ``data`` (samples x
# 384 channels, int16, gzip level 4, chunks of 13,020 samples, 10 MB) and
# ``timestamps`` (float64 seconds, one per sample); both stay references.
#
# Two things are added to what NWB stores, both ignored by NWBZarrIO:
# ``dimension_names`` on every array, for xarray, and on each voltage array
# CF ``scale_factor``, ``add_offset`` and ``units``, copied from NWB's
# ``conversion``, ``offset`` and ``unit``, so xarray returns volts as pynwb
# does. The SpikeGLX ``.meta`` of each band, which states the nominal sampling
# rate, gains and probe serial number, goes on its ElectricalSeries group.

NWB_FILES = ("raw", "processed")


def write_nwb_file(session_, group: str, nwb: DandiFile) -> None:
    """Write one NWB file whole at ``group``, its chunks referenced at DANDI.

    shadowzarr reads the headers over HTTPS; the references name the ``s3://``
    location of the same object, which the repo's virtual chunk container
    covers.
    """
    rfs = name_dimensions(under_group(nwb_references(nwb.https), group))
    for key, value in rfs["refs"].items():
        parent = rfs["refs"].get(key.removesuffix("/data/zarr.json") + "/zarr.json", "")
        if key.endswith("/data/zarr.json") and '"neurodata_type":"ElectricalSeries"' in parent:
            rfs["refs"][key] = with_cf_scale(value)
    write_nwb(rfs, session_, url_for=lambda url: as_url(url.replace(
        "https://dandiarchive.s3.amazonaws.com/", "s3://dandiarchive/")))


def with_cf_scale(metadata: str) -> str:
    meta = json.loads(metadata)
    attrs = meta["attributes"]
    if attrs.get("unit") != "volts":
        raise ValueError(f"an ElectricalSeries in {attrs.get('unit')!r}, not volts")
    attrs.update({"scale_factor": attrs["conversion"], "add_offset": attrs["offset"],
                  "units": "V", "coordinates": "timestamps electrodes"})
    return json.dumps(meta, separators=(",", ":"))


def spikeglx_meta(session: dict, probe: str, band: str) -> dict:
    """The band's SpikeGLX ``.meta`` file as a dict of its ``key=value`` lines."""
    datasets = one_cache(SORTS["2024"]["cache"])["datasets"].reset_index()
    rows = datasets[(datasets.eid == session["eid"])
                    & datasets.rel_path.str.match(rf"raw_ephys_data/{probe}/.*\.{band}\.meta$")]
    if len(rows) != 1:
        raise LookupError(f"{len(rows)} {band} .meta files for {session['group']} {probe}")
    row = rows.iloc[0]
    text = read_range(bucket_url(session["session_path"], row.rel_path, row.id), 0,
                      int(row.file_size)).decode()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


# Spike sorting
# -------------
# The IBL bucket stores a sort as ALF: one uncompressed ``.npy`` per
# attribute, named ``<object>.<attribute>``, every attribute of an object
# sharing its first axis. Each object becomes a group, each attribute an array
# referencing the file past its 128-byte header. Per-spike columns hold up to
# tens of millions of rows, so they are chunked every 2**18 rows (2 MB of
# float64); the short last chunk of each is written natively. Per-cluster and
# per-channel files are one chunk each.
#
# Left out of phase 1: ``drift``, ``waveforms.*`` and ``_phy_spikes_subset.*``
# (raw waveform snippets, 2 to 3 GB per probe), and the ``clusters.metrics``
# Parquet table, which has no Zarr convention and belongs in Iceberg.

OBJECTS = {"spikes": "spike", "clusters": "cluster", "channels": "channel", "templates": "template"}

# The axes after the first, per attribute, from the shapes in the files.
EXTRA_AXES = {
    "channels.localCoordinates": ("probe_axis",),
    "channels.mlapdv": ("mlapdv_axis",),
    "clusters.waveforms": ("sample", "waveform_channel"),
    "clusters.waveformsChannels": ("waveform_channel",),
    "templates.waveforms": ("sample", "waveform_channel"),
    "templates.waveformsChannels": ("waveform_channel",),
}

SPIKE_ROWS_PER_CHUNK = 2**18


def sort_files(eid: str, probe: str, sort: str):
    """The ALF files of one probe's sort: ``[(object, attribute, url, size, md5), ...]``."""
    spec = SORTS[sort]
    cache = one_cache(spec["cache"])
    datasets = cache["datasets"].reset_index()
    prefix = f"alf/{probe}/pykilosort/{spec['revision']}"
    rows = datasets[(datasets.eid == eid) & datasets.rel_path.str.startswith(prefix)]
    session_path = session_record(eid)["session_path"]
    files = []
    for row in rows.itertuples():
        name = row.rel_path[len(prefix):]
        if "/" in name or not name.endswith(".npy"):
            continue
        obj, attribute = name.removesuffix(".npy").split(".", 1)
        if obj in OBJECTS:
            files.append((obj, attribute, bucket_url(session_path, row.rel_path, row.id),
                          int(row.file_size), row.hash))
    if not files:
        raise LookupError(f"no {sort} sort for {eid} {probe} under {prefix}")
    return files


def spike_sorting(eid: str, probe: str, sort: str):
    """The sort as one virtual group per ALF object, the short tails to write natively,
    and the ETag to pin each array's references to.

    Returns ``(ManifestGroup, [(object, attribute, first_row, url, start, end, dtype)],
    {"<object>/<attribute>": (ManifestArray, etag)})``.
    """
    groups: dict[str, dict[str, ManifestArray]] = {}
    tails = []
    pins = {}
    for obj, attribute, url, size, md5 in sort_files(eid, probe, sort):
        shape, dtype, offset = npy_header(url)
        if offset + int(np.prod(shape)) * dtype.itemsize != size:
            raise ValueError(f"{url}: header says {shape} {dtype}, file is {size} bytes")
        rows = SPIKE_ROWS_PER_CHUNK if obj == "spikes" else max(shape[0], 1)
        dims = (OBJECTS[obj], *EXTRA_AXES.get(f"{obj}.{attribute}", ()))
        if len(dims) != len(shape):
            raise ValueError(f"{url}: shape {shape} does not match axes {dims}")
        array = flat_manifest_array(
            url, shape=shape, dtype=dtype.newbyteorder("="), data_offset=offset,
            endian="big" if dtype.byteorder == ">" else "little", rows_per_chunk=rows,
            dimension_names=dims, attributes={"ibl_dataset": f"{obj}.{attribute}"})
        pins[f"{obj}/{attribute}"] = (array, pinned_etag(url, md5))
        array.metadata.attributes["source_etag"] = str(pins[f"{obj}/{attribute}"][1])
        if attribute == "times" or attribute.endswith("_times"):
            # ALF: timestamp datasets share the session's timescale, in seconds.
            # A CF units string, set after construction: manifest_array reads
            # ``units`` as OME-Zarr's per-axis mapping.
            array.metadata.attributes["units"] = "s"
        groups.setdefault(obj, {})[attribute] = array
        if tail := flat_tail(shape=shape, dtype=dtype, data_offset=offset, rows_per_chunk=rows):
            tails.append((obj, attribute, tail[0], url, tail[1], tail[2], dtype))
    revision = SORTS[sort]["revision"].strip("/#") or "default (no revision folder)"
    group = ManifestGroup(
        arrays={},
        groups={obj: ManifestGroup(arrays=arrays, attributes={"alf_object": obj})
                for obj, arrays in groups.items()},
        attributes={
            "description": "Kilosort 2.5 (pykilosort) output in IBL's ALF layout",
            "sort": sort,
            "ibl_revision": revision,
            "ibl_release_cache": SORTS[sort]["cache"],
        },
    )
    return group, tails, pins


def clusters_uuids(eid: str, probe: str, sort: str) -> np.ndarray:
    """Each cluster's UUID, read from ``clusters.uuids.csv``, which holds strings."""
    spec = SORTS[sort]
    datasets = one_cache(spec["cache"])["datasets"].reset_index()
    rel = f"alf/{probe}/pykilosort/{spec['revision']}clusters.uuids.csv"
    row = datasets[(datasets.eid == eid) & (datasets.rel_path == rel)].iloc[0]
    url = bucket_url(session_record(eid)["session_path"], rel, row.id)
    text = read_range(url, 0, int(row.file_size)).decode()
    return np.array([r["uuids"] for r in csv.DictReader(io.StringIO(text))])


# Writing the store
# -----------------
# The root says what the store is and where every byte comes from. A new sort
# replaces the probe's whole ``pykilosort`` group, because its arrays change
# length; the cluster UUIDs and the short tails of the spike columns are small
# native arrays written with zarr.

ROOT_ATTRS = {
    "title": "IBL Brain-Wide Map: Neuropixels recordings, behavior and spike sorting",
    "description": (
        "Neuropixels recordings from mice performing a visual decision task, recorded by "
        "the International Brain Laboratory. Each session is a group sub-<subject>/ses-<eid>, "
        "named as on DANDI. In it, raw and processed are DANDI's two NWB files for the "
        "session, each an NWBFile group in the NWB Zarr v3 convention of hdmf-zarr 0.14, and "
        "alf/<probe>/pykilosort is IBL's spike sorting as ALF objects. Every array names its "
        "dimensions, so zarr and xarray read any group. hdmf-zarr's NWBZarrIO opens only a "
        "store's root, so it cannot yet open a session's NWB group in this store. Arrays "
        "reference the original files on DANDI and in the IBL public bucket."
    ),
    "license": "CC-BY-4.0",
    "citation": [
        "International Brain Laboratory et al. (2025) A brain-wide map of neural activity during "
        "complex behaviour. Nature. https://doi.org/10.1038/s41586-025-09235-0",
        "IBL - Brain Wide Map, DANDI 000409. https://doi.org/10.48324/dandi.000409/0.260309.1324",
    ],
    "sources": {
        "nwb": "https://dandiarchive.org/dandiset/000409/0.260309.1324",
        "spike_sorting": "s3://ibl-brain-wide-map-public/data/",
    },
    "conventions": (
        "NWB files in the unified NWB Zarr v3 convention (hdmf-zarr 0.14); xarray "
        "dimension_names on every array; CF scale_factor/add_offset/units on voltages. "
        "Codecs: bytes, zstd and vlen-utf8 from the Zarr spec, and numcodecs.zlib, the HDF5 "
        "gzip filter, which numcodecs provides and zarr-python installs"
    ),
}


def write_sorting(store, session: dict, probe: str, sort: str) -> None:
    path = f"{session['group']}/alf/{probe}"
    sorting, tails, pins = spike_sorting(session["eid"], probe, sort)
    probe_group = zarr.open_group(store, path=path, mode="a")
    if "pykilosort" in probe_group:
        del probe_group["pykilosort"]
    sorting.to_icechunk(store, group=f"{path}/pykilosort", mode="w-")
    for name, (array, etag) in pins.items():
        pin_virtual_refs(store, f"{path}/pykilosort/{name}", array, etag)
    for obj, attribute, first, url, start, end, dtype in tails:
        array = zarr.open_array(store, path=f"{path}/pykilosort/{obj}/{attribute}", mode="a")
        array[first:] = np.frombuffer(read_range(url, start, end), dtype=dtype)
    clusters = zarr.open_group(store, path=f"{path}/pykilosort/clusters", mode="a")
    uuids = clusters_uuids(session["eid"], probe, sort)
    count = next(iter(clusters.arrays()))[1].shape[0]
    if len(uuids) != count:
        raise ValueError(f"{path}: {len(uuids)} cluster UUIDs for {count} clusters")
    array = clusters.create_array("uuids", shape=uuids.shape, dtype=str,
                                  dimension_names=("cluster",), overwrite=True)
    array[:] = uuids
    probe_group.attrs.update({"description": f"IBL's ALF collection alf/{probe}"})


def held_sorts(repo, session: dict, probes: list[str]) -> list[str | None]:
    """The sort ``main`` holds for each probe of the session, None where there is none."""
    try:
        group = zarr.open_group(repo.readonly_session("main").store, path=session["group"], mode="r")
    except zarr.errors.GroupNotFoundError:
        return [None] * len(probes)
    return [group[f"alf/{probe}/pykilosort"].attrs.get("sort")
            if f"alf/{probe}/pykilosort" in group else None for probe in probes]


def written(repo, session: dict, probes: list[str], sort: str, sorting_only: bool) -> bool:
    """Whether ``main`` already holds this session as far as this pass would take it.

    A session holding this pass's sort or a later one counts as written, so a
    relaunched pass never writes an older sort over a newer one.
    """
    later = list(SORTS)[list(SORTS).index(sort):]
    held = held_sorts(repo, session, probes)
    if not all(h in later for h in held):
        return False
    if sorting_only:
        return True
    group = zarr.open_group(repo.readonly_session("main").store, path=session["group"], mode="r")
    return all(name in group for name in NWB_FILES)


def write_session(repo, eid: str, probes: list[str], sort: str, sorting_only: bool) -> str:
    """Write one session in its own commit; return the snapshot id.

    A commit per session keeps each commit to a few hundred arrays, and lets a
    run that stops resume where it was.
    """
    session = session_record(eid)
    session_ = repo.writable_session("main")
    store = session_.store
    zarr.open_group(store, mode="a").attrs.update(ROOT_ATTRS)
    attrs = {k: session[k] for k in ("eid", "lab", "subject", "date", "number",
                                     "task_protocol", "session_path")}
    attrs["description"] = (
        f"IBL session {eid}: subject {session['subject']}, {session['lab']}, "
        f"{session['date']} number {session['number']}. raw and processed are DANDI's "
        "raw and processed NWB files; alf holds IBL's spike sorting per probe.")
    subject = session["group"].split("/")[0]
    zarr.open_group(store, path=subject, mode="a").attrs.update({
        "subject": session["subject"],
        "description": f"IBL subject {session['subject']} (DANDI {subject}): one group per "
                       "recording session, ses-<eid>"})
    zarr.open_group(store, path=session["group"], mode="a")
    zarr.open_group(store, path=f"{session['group']}/alf", mode="a").attrs["description"] = (
        "IBL's spike sorting for each probe, at IBL's own collection path "
        "alf/<probe>/pykilosort, as ALF objects (spikes, clusters, channels, templates)")
    if not sorting_only:
        for name in NWB_FILES:
            nwb = dandi_file(eid, name)
            write_nwb_file(session_, f"{session['group']}/{name}", nwb)
            attrs[f"{name}_nwb"] = {"dandi_path": nwb.path, "url": nwb.url,
                                    "sha256": nwb.sha256, "size": nwb.size}
            say(f"  {name} NWB written: {nwb.path} ({nwb.size / 1e9:.1f} GB)")
        for probe in probes:
            for band in ("ap", "lf"):
                series = zarr.open_group(
                    store, mode="a",
                    path=f"{session['group']}/raw/acquisition/ElectricalSeries"
                         f"{probe.capitalize()}{band.upper()}")
                series.attrs["spikeglx_meta"] = spikeglx_meta(session, probe, band)
    for probe in probes:
        write_sorting(store, session, probe, sort)
        say(f"  {probe} {sort} sort written")
    zarr.open_group(store, path=session["group"], mode="a").attrs.update(attrs)
    revision = SORTS[sort]["revision"].strip("/") or "the default revision"
    message = (f"IBL Brain-Wide Map: {session['group']}: "
               + (f"the {sort} spike sorting ({revision})" if sorting_only else
                  f"the raw and processed NWB files and the {sort} spike sorting ({revision})"))
    started = time.monotonic()
    snapshot = commit(session_, message, metadata={
        "session": eid, "sort": sort, "probes": probes, "nwb": not sorting_only})
    say(f"  committed {snapshot} in {time.monotonic() - started:.1f} s")
    return snapshot


def run_pass(repo, eids: list[str], probes: list[str], sort: str, sorting_only: bool,
             tag: str | None) -> None:
    """Write every session of one pass, a commit each, then tag the branch tip.

    A pass whose tag already exists in the repo is done and is skipped whole;
    otherwise each session already written (:func:`written`) is skipped. So a
    run stopped anywhere can be launched again and picks up where it stopped.
    """
    if tag and tag in repo.list_tags():
        say(f"{sort} pass: tag {tag} exists, so the pass is done; skipped")
        return
    started = time.monotonic()
    for done, eid in enumerate(eids):
        session = session_record(eid)
        say(f"{sort} pass, session {done + 1} of {len(eids)}: {session['group']} "
            f"({time.monotonic() - started:.0f} s so far, {len(eids) - done} to go)")
        if written(repo, session, probes, sort, sorting_only):
            say("  already on main; skipped")
            continue
        if sorting_only and not written(repo, session, probes, list(SORTS)[0], False):
            raise RuntimeError(f"{session['group']} has no NWB files on main; run the first pass first")
        write_session(repo, eid, probes, sort, sorting_only)
    if tag:
        held = {eid: held_sorts(repo, session_record(eid), probes) for eid in eids}
        wrong = {eid: h for eid, h in held.items() if any(s != sort for s in h)}
        if wrong:
            raise RuntimeError(f"not tagging {tag}: main holds other sorts for {wrong}")
        tip = repo.lookup_branch("main")
        repo.create_tag(tag, snapshot_id=tip)
        say(f"tagged {tip} as {tag}")
    say(f"{sort} pass done: {len(eids)} session(s) in {time.monotonic() - started:.0f} s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sort", choices=sorted(SORTS), required=True)
    parser.add_argument("--sessions", default=",".join(SESSIONS))
    parser.add_argument("--probes", default=",".join(PROBES))
    parser.add_argument("--sorting-only", action="store_true",
                        help="write only the spike sorting, over existing NWB groups")
    parser.add_argument("--tag", help="tag the branch tip with this name when every session is "
                                      "written; a pass whose tag exists is skipped")
    args = parser.parse_args()
    run_pass(open_repo(args.repo), args.sessions.split(","), args.probes.split(","),
             args.sort, args.sorting_only, args.tag)


if __name__ == "__main__":
    main()
