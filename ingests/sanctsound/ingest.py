"""Virtualize NOAA's SanctSound hydrophone archive into Icechunk.

The audio stays in NOAA's bucket. The store holds byte-range references into the
FLAC recordings, with NOAA's detections, hourly sound levels, temperature record
and calibration certificate copied beside them on one clock. LAYOUTS.md says what
the archive contains and how it maps onto the store; this file is the code.

One group per deployment (``CI01_01``), holding:

    audio/waveform        every recording end to end, ("time",)
    audio/segments/       one row per recording: sample <-> wall-clock index
    detections/           one event table across NOAA's detection files
    sound_levels/         BB, OL, TOL and PSD on one hourly axis
    temperature/          the mooring's logger record, where NOAA published one
    calibration/          the certificate PDF and the counts-to-pressure offset

Two steps, run as subcommands:

    scan DEPLOYMENT [START/END ...]   read every frame offset into a chunk table
    build DEPLOYMENT --repo PATH      write the deployment from that table

The scan reads every byte of every recording, so run it beside the data. The
chunk table it leaves behind is small (about 1.3 MB for 175,000 chunks) and lets
a rebuild skip the audio entirely.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import obstore
import zarr
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import GCSStore

from minus80.core.progress import Progress
from minus80.core.registry import CLIENT_OPTIONS, RETRY_CONFIG
from minus80.core.remote import reader
from minus80.parsers.flac import ChunkTable, FrameScan, chunk_table, scan_frames, write_array

BUCKET = "noaa-passive-bioacoustic"
BASE = f"gs://{BUCKET}/"
GCS = f"https://storage.googleapis.com/{BUCKET}/"
CODEC_URL = "https://github.com/ianhi/flac-numcodecs/tree/zarr3-flac-codec-2026-10-19"

# Reproduces the chunking of the published earthmover-biology/sanctsound store,
# where a chunk is 512 FLAC frames. The parser's default is one frame per chunk, the
# file's own storage unit, and merging frames is a choice this ingest has not
# settled: 512 frames coarsens every read to about 22 s at 96 kHz but keeps
# 175,314 references for CI01_01 instead of about 90 million.
FRAMES_PER_CHUNK = 512

# NOAA's bucket allows anonymous reads. The retry settings are core's, for a
# public archive that drops connections.
BUCKET_STORE = GCSStore(BUCKET, skip_signature=True, client_options=CLIENT_OPTIONS,
                        retry_config=RETRY_CONFIG)
BUCKET_REGISTRY = ObjectStoreRegistry({BASE: BUCKET_STORE})


def list_keys(prefix: str, suffix: str) -> list[tuple[str, int]]:
    """Every key under ``prefix`` that ends in ``suffix``, with its size in bytes."""
    keys = []
    for batch in obstore.list(BUCKET_STORE, prefix=prefix):
        for item in batch:
            if item["path"].endswith(suffix):
                keys.append((item["path"], item["size"]))
    return sorted(keys)


def read_object(key: str) -> bytes:
    """The whole object, fetched in 16 MB ranges.

    Product files run to 600 MB, and a single long GET is cut short often enough
    to matter. Each range is retried on its own, so a dropped connection costs
    one range.
    """
    return reader(BASE + key, registry=BUCKET_REGISTRY, block=16 << 20,
                  cached_blocks=1).read()


def deployment_prefix(site: str, dep: str) -> str:
    return f"sanctsound/audio/{site.lower()}/sanctsound_{dep.lower()}/"


# Each filename form the ingest reads, with the strptime format of its date and
# time digits. LAYOUTS.md lists the forms it does not read yet.
FILENAME_TIMES = [
    # 20181031T220000Z (CI01_01, SB01_03). SB01_03's first and last files add
    # _Post-Deployment or _Pre-Retrieval, but belong to the continuous series.
    (re.compile(r"_(\d{8})T(\d{6})Z(?:_[A-Za-z-]+)?\.flac$"), "%Y%m%d%H%M%S"),
    # 201122183500: a two-digit year, the most common form.
    (re.compile(r"_(\d{6})(\d{6})\.flac$"), "%y%m%d%H%M%S"),
]


def parse_start(name: str) -> datetime:
    """Start time from the filename. The FLAC files carry no tags at all."""
    for pattern, fmt in FILENAME_TIMES:
        if match := pattern.search(name):
            return datetime.strptime("".join(match.groups()), fmt).replace(tzinfo=timezone.utc)
    raise ValueError(f"no timestamp pattern matches {name!r}")


@dataclass(frozen=True)
class DeploymentTable:
    """A deployment's chunk table with what the parser's table does not carry.

    ``start`` is each recording's start time in nanoseconds since the epoch, from
    its filename. ``windows`` and ``n_listed`` are set when only the recordings
    around some time windows were scanned.
    """

    chunks: ChunkTable
    start: np.ndarray
    windows: tuple[str, ...] = ()
    n_listed: int | None = None

    def save(self, path) -> None:
        """Write the parser's table fields plus ``start`` to one ``.npz``."""
        extra = dict(windows=np.array(self.windows), n_listed=self.n_listed) \
            if self.windows else {}
        np.savez_compressed(path, **asdict(self.chunks), start=self.start, **extra)

    @classmethod
    def load(cls, path) -> DeploymentTable:
        """Read a saved table. A file with no ``channels`` field is mono.

        ``ci01_01_chunks.npz`` and its siblings predate the ``channels`` field,
        and every SanctSound recording is mono.
        """
        with np.load(path, allow_pickle=False) as z:
            fields = {k: z[k] for k in ChunkTable.__dataclass_fields__ if k in z}
            fields.setdefault("channels", 1)
            fields["sample_rate"] = int(fields["sample_rate"])
            fields["blocksize"] = int(fields["blocksize"])
            fields["channels"] = int(fields["channels"])
            windows = tuple(str(w) for w in z["windows"]) if "windows" in z else ()
            return cls(ChunkTable(**fields), z["start"].astype("int64"), windows,
                       int(z["n_listed"]) if "n_listed" in z else None)


def scan_recording(key: str) -> FrameScan:
    """Scan one recording over HTTPS, recording its ``gs://`` URL."""
    return scan_frames(GCS + key, url=BASE + key)


def deployment_table(items, scans, *, windows=(), n_listed=None) -> DeploymentTable:
    table = chunk_table(scans, frames_per_chunk=FRAMES_PER_CHUNK)
    start = np.array([int(parse_start(key).timestamp()) * 10**9 for key, _ in items],
                     "int64")
    return DeploymentTable(table, start, tuple(windows), n_listed)


def overlapping(items, windows) -> list[int]:
    """Indices of the recordings that overlap any window. A recording is taken to run
    until the next one starts; the last is kept if a window starts after it."""
    starts = [parse_start(key) for key, _ in items]
    ends = starts[1:] + [datetime.max.replace(tzinfo=timezone.utc)]
    return [i for i, (a, b) in enumerate(zip(starts, ends))
            if any(a < hi and lo < b for lo, hi in windows)]


def parse_window(text: str):
    lo, hi = (datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
              for t in text.split("/"))
    return lo, hi


def scan_deployment(dep: str, window_args: list[str], cache: Path, out: Path) -> None:
    """Scan a deployment's recordings and write its chunk table to ``out``.

    With windows, only the recordings that overlap one of them are scanned, and
    the table records the windows so the store can say its audio is partial. Each
    recording lands in its own file under ``cache``, so an interrupted run resumes
    where it stopped.
    """
    dep = dep.upper()
    windows = [parse_window(w) for w in window_args]
    cache.mkdir(parents=True, exist_ok=True)
    items = list_keys(deployment_prefix(dep[:4], dep) + "audio/", ".flac")
    print(f"{dep}: listed {len(items)} files, {sum(s for _, s in items) / 1e9:.1f} GB",
          flush=True)
    keep = overlapping(items, windows) if windows else list(range(len(items)))
    if windows:
        print(f"{len(keep)} overlap the windows, "
              f"{sum(items[i][1] for i in keep) / 1e9:.1f} GB", flush=True)

    def cached(i):
        return cache / f"{i:04d}.npz"

    todo = [i for i in keep if not cached(i).exists()]
    print(f"{len(keep) - len(todo)} already cached, {len(todo)} to scan", flush=True)
    # Four workers saturate a cloud VM's bandwidth; eight are slower than four.
    with Progress(f"scanning {len(todo)} recordings", sum(items[i][1] for i in todo),
                  unit="GB", scale=1e9, every=30) as progress, \
            ThreadPoolExecutor(max_workers=4) as pool:
        for i, scan in zip(todo, pool.map(lambda i: scan_recording(items[i][0]), todo)):
            scan.save(cached(i))
            progress.advance(items[i][1])
    kept = [items[i] for i in keep]
    table = deployment_table(kept, [FrameScan.load(cached(i)) for i in keep],
                             windows=window_args, n_listed=len(items))
    table.save(out)
    print(f"scan complete; chunk table in {out}", flush=True)



EPOCH = dict(units="nanoseconds since 1970-01-01T00:00:00Z",
             calendar="proleptic_gregorian")


def write_small_array(group, name, data, dims, **attrs):
    """Write a small materialized array in one chunk."""
    data = np.asarray(data)
    return group.create_array(name, data=data, chunks=data.shape or "auto",
                              dimension_names=dims, attributes=attrs, overwrite=True)


def write_audio(group, table: DeploymentTable):
    """(Re)create the waveform, the segment table and the time coverage.

    Growth is a rebuild rather than an append: recreating the metadata with the
    full edge list takes about 0.4 s for 382,000 edges, against about 2 min of
    ``resize()`` calls.
    """
    chunks = table.chunks
    sample_rate = int(chunks.sample_rate)
    uris = [str(u) for u in chunks.source_uri]
    audio = group.require_group("audio")
    write_array(
        audio, "waveform", chunks, channel_axis=False,
        attributes=dict(
            long_name="hydrophone signal, every recording end to end",
            sample_rate_hz=sample_rate, bits_per_sample=16,
            blocksize=int(chunks.blocksize), units="1",
            processing_level="raw uncalibrated counts",
            coverage_content_type="physicalMeasurement",
            time_index="audio/segments",
            comment="int16 counts, not sound pressure: add calibration/counts_to_upa_db "
                    "to 20*log10(|counts|) for dB re 1 uPa. Every recording is mono, so "
                    "the waveform has no channel axis. Chunks are byte-range "
                    "references into NOAA's FLAC; reading needs the 'flac' Zarr codec "
                    "from flac-numcodecs, tag zarr3-flac-codec-2026-10-19."))
    audio.attrs.update(
        title="Hydrophone audio",
        comment="waveform packs every recording end to end on one sample axis; "
                "segments maps sample index to wall-clock time, one row per "
                "recording.")
    seg = audio.require_group("segments")
    ns = np.asarray(chunks.n_samples, "int64")
    starts = chunks.start_sample
    t0s = np.asarray(table.start, "int64")
    ends = t0s + ns * 10**9 // sample_rate
    aux = dict(coverage_content_type="auxiliaryInformation")
    write_small_array(seg, "start_sample", starts, ("segment",), **aux,
         long_name="index into audio/waveform of this recording's first sample")
    write_small_array(seg, "n_samples", ns, ("segment",), **aux,
         long_name="number of samples in this recording", sample_dimension="time")
    write_small_array(seg, "start_time", t0s, ("segment",), **aux, **EPOCH,
         standard_name="time", long_name="recording start")
    write_small_array(seg, "end_time", ends, ("segment",), **aux, **EPOCH,
         long_name="start_time + n_samples/sample_rate_hz")
    write_small_array(seg, "source_uri", np.array(uris, "<U160"), ("segment",),
         long_name="object these samples are byte-range references into")
    overlap = np.full(len(ns), np.nan, dtype="float32")
    overlap[:-1] = (ends[:-1] - t0s[1:]) / 1e9
    write_small_array(seg, "overlap_seconds", overlap, ("segment",), **aux, units="s",
         long_name="apparent overlap with the next recording; negative is a gap",
         comment="start_time[k+1] subtracted from end_time[k]. A positive value is "
                 "NOT duplicated audio: correlating the disputed samples across a "
                 "boundary gives r = 0.005, so the recording is continuous and the "
                 "filename timestamps are imprecise by up to ~1.5 s. Use this to "
                 "judge timestamp confidence, never to trim samples. NaN on the "
                 "last segment.")
    seg.attrs.update(featureType="timeSeries", comment=(
        "The waveform holds every recording end to end, with no gaps between them. "
        "Each row of audio/segments describes one recording: its first sample in the "
        "waveform (start_sample), its length in samples (n_samples), and when it "
        "started (start_time).\n\n"
        "To find when sample i was recorded, find the row where start_sample <= i < "
        "start_sample + n_samples. The time is start_time + (i - start_sample) / "
        "sample_rate_hz.\n\n"
        "To find the sample recorded at time t, find the row where start_time <= t < "
        "end_time. If no row contains t, nothing was recorded at that moment.\n\n"
        "Start times come from the file names and are accurate to about 1.5 seconds. "
        "Times within one recording are exact."))
    if table.windows:
        audio.attrs.update(
            audio_selection=list(table.windows),
            audio_selection_comment=(
                f"Only the {len(ns)} of this deployment's {table.n_listed} "
                "recordings that overlap audio_selection (UTC start/end) are referenced. "
                "The detections and sound levels cover the whole deployment; audio "
                "outside these recordings is in NOAA's bucket but not in this store."))

    group.attrs.update(
        time_coverage_start=str(np.datetime64(int(t0s.min()), "ns")),
        time_coverage_end=str(np.datetime64(int(ends.max()), "ns")))



SPL_DIM = {"BB_1h": None, "OL_1h": "frequency_octave",
           "TOL_1h": "frequency_third_octave", "PSD_1h": "frequency_psd"}
SPL_VAR = {"BB_1h": "broadband_spl", "OL_1h": "octave_spl",
           "TOL_1h": "third_octave_spl", "PSD_1h": "psd"}
SPL_STANDARD_NAME = "sound_pressure_level_in_water"
# The reference quantity CF defines each name against, for explaining a correction.
CF_REFERENCE = {"sound_intensity_level_in_water": "6.7e-19 W m-2",
                "sound_pressure_level_in_water": "1e-6 Pa"}
# Every detection label in SanctSound, by base label: NOAA's label without its
# `_1d`/`_1h`/`_1hr`/`_1min`/`_manual` resolution or method suffix. Biological ones
# carry a CF 6.1.2 taxon, with the WoRMS AphiaID looked up by name (2026-09-24).
TAXON = {
    "bluewhale": ("Balaenoptera musculus", 137090),
    "finwhale": ("Balaenoptera physalus", 137091),
    "humpbackwhale": ("Megaptera novaeangliae", 137092),
    "GoogleAI_humpbackwhale": ("Megaptera novaeangliae", 137092),
    "seiwhale": ("Balaenoptera borealis", 137088),
    "minkewhale": ("Balaenoptera acutorostrata", 137087),
    "northatlanticrightwhale": ("Eubalaena glacialis", 159023),
    "killerwhale": ("Orcinus orca", 137102),
    "dolphins": ("Delphinidae", 136980),
    "pinnipeds": ("Pinnipedia", 148736),
    "bocaccio": ("Sebastes paucispinis", 274833),
    "plainfinmidshipman": ("Porichthys notatus", 275658),
    "atlanticcod": ("Gadus morhua", 126436),
    "redgrouper": ("Epinephelus morio", 159354),
    "blackgrouper": ("Mycteroperca bonaci", 159231),
}
# MFA is mid-frequency active sonar: the `sonar` files name their variable
# `mfa_presence`.
ANTHROPOGENIC = {"Explosions", "ships", "sonar", "MFA"}
UNKNOWN_SOURCE = {"unknownimpulse"}


def base_label(label: str) -> str:
    return re.sub(r"_(1d|1h|1hr|1min|manual)$", "", label)


def sound_source(label: str) -> tuple[str, str, str]:
    """`(source_type, taxon name, taxon LSID)` for a detection label.

    An unknown label raises: a default here would publish a guess as a fact.
    """
    base = base_label(label)
    if base in TAXON:
        name, aphia = TAXON[base]
        return "biological", name, f"urn:lsid:marinespecies.org:taxname:{aphia}"
    if base in ANTHROPOGENIC:
        return "anthropogenic", "", ""
    if base in UNKNOWN_SOURCE:
        return "unknown", "", ""
    raise KeyError(f"detection label {label!r} is not in TAXON, ANTHROPOGENIC or "
                   f"UNKNOWN_SOURCE")


def product_keys(product: str, site: str, dep: str) -> list[str]:
    keys = list_keys(f"sanctsound/products/{product}/{site.lower()}/", ".nc")
    return [k for k, _ in keys if f"_{dep}_" in k]


def product_label(key: str, dep: str) -> str:
    """`.../SanctSound_CI01_01_BB_1h.nc` -> `BB_1h`."""
    return key.split("/")[-1].removesuffix(".nc").removeprefix(f"SanctSound_{dep}_")


def open_netcdf(key):
    import h5py

    return h5py.File(io.BytesIO(read_object(key)), "r")


def as_text(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def source_doi(f) -> str:
    """The bare DOI from a product file's `id`, which NOAA writes as an http URL."""
    return as_text(f.attrs["id"]).split("doi.org/")[-1]


def source_history(f) -> str:
    """NOAA writes `history` as a one-element Python list literal; unwrap it."""
    h = as_text(f.attrs.get("history", ""))
    if h.startswith('["') and h.endswith('"]'):
        h = h[2:-2]
    return h


def write_temperature(group, site: str, dep: str):
    """The mooring's temperature logger record, from NOAA's original .rsk file.

    Materialized, not referenced: an .rsk is an SQLite database, so its values have
    no byte ranges a Zarr chunk could point at. Returns the sample count, or None
    when NOAA published no logger record for the deployment.
    """
    from pyrsktools import RSK

    keys = [k for k, _ in list_keys(deployment_prefix(site, dep) + "ancillary/temperature/",
                                ".rsk")]
    if not keys:
        return None
    if len(keys) != 1:
        raise ValueError(f"{dep}: expected one .rsk temperature record, found {keys}")
    with tempfile.NamedTemporaryFile(suffix=".rsk") as fh:
        fh.write(read_object(keys[0]))
        fh.flush()
        with RSK(fh.name) as rsk:
            rsk.readdata()
            data, inst, deployment = rsk.data, rsk.instrument, rsk.deployment
            (channel,) = rsk.channels

    t = group.require_group("temperature")
    day = 86400 // int(np.median(np.diff(data["timestamp"][:1000]))
                       .astype("timedelta64[s]").astype(int))
    for name, values, dims, attrs in [
        ("time", data["timestamp"].astype("datetime64[ns]").astype("int64"), ("time",),
         dict(standard_name="time", long_name="logger timestamp", **EPOCH)),
        ("sea_water_temperature", data["temperature"], ("time",),
         dict(standard_name="sea_water_temperature", units="degree_Celsius",
              long_name=f"{channel.longNamePlainText} ({channel.shortName})",
              coordinates="time")),
    ]:
        t.create_array(name, data=np.asarray(values), chunks=(min(day, len(values)),),
                       dimension_names=dims, attributes=attrs, overwrite=True)
    t.attrs.update(
        title="Mooring temperature",
        featureType="timeSeries",
        instrument=f"{inst.model} temperature logger",
        instrument_serial_number=str(inst.serialID),
        source_uri=f"gs://{BUCKET}/{keys[0]}",
        logger_time_drift_ms=int(deployment.loggerTimeDrift),
        logger_time_drift_comment="RBR's loggerTimeDrift field for this record, as "
                                  "the .rsk reports it; the timestamps are not "
                                  "corrected for it.",
        time_of_download=str(deployment.timeOfDownload),
        comment="The logger's full record, read with pyrsktools. It starts before "
                "the mooring went in and ends after it came out; the values outside "
                "the deployment period were not logged at depth.")
    return len(data)


def write_products(group, site: str, dep: str):
    """Materialize the sound levels and detections.

    These are ~205 MB total and the source chunking is unusable: PSD_1h is one
    contiguous 204 MB block, so virtualizing it would make every frequency query
    pull the whole file. Virtualize what is big; copy what is small.
    """
    from zarr.codecs.numcodecs import Zstd

    level_keys = product_keys("sound_level_metrics", site, dep)
    # NOAA did not publish sound levels for every deployment (none for OC01_01).
    sl = group.require_group("sound_levels") if level_keys else None
    for key in level_keys:
        label = product_label(key, dep)
        with open_netcdf(key) as f:
            # Every product carries the same hourly axis; the last one written wins.
            write_small_array(sl, "time", f["time"][()].astype("int64") * 10**9, ("time",),
                 standard_name="time", **EPOCH,
                 comment=as_text(f["time"].attrs["comment"]))
            fdim = SPL_DIM[label]
            if fdim:
                write_small_array(sl, fdim, f["frequency"][()].astype("float64"), (fdim,),
                     standard_name="sound_frequency", units="Hz")
            spl = f["sound_pressure_levels"][()]
            src = f["sound_pressure_levels"].attrs
            # The source declares `time: sum`, which contradicts its own history:
            # every product is an hourly median of 1-second values.
            comment = (f"cell_methods follow the method stated in source_history. The "
                       f"source declares cell_methods={as_text(src['cell_methods'])!r}.")
            declared = as_text(src["standard_name"])
            if declared != SPL_STANDARD_NAME:
                why = (f" (CF defines that against {CF_REFERENCE[declared]})"
                       if declared in CF_REFERENCE else "")
                comment += (
                    f" The source also declares standard_name={declared!r}{why}. "
                    f"Corrected here to {SPL_STANDARD_NAME}, matching the source's own "
                    f"long_name and the other sound-level files. CF standard name table "
                    f"v94: "
                    f"https://cfconventions.org/Data/cf-standard-names/current/src/"
                    f"cf-standard-name-table.xml")
            sl.create_array(
                SPL_VAR[label], data=spl,
                chunks=(256, 4096) if label == "PSD_1h" else spl.shape,
                dimension_names=("time",) if fdim is None else ("time", fdim),
                compressors=[Zstd(level=5)], overwrite=True,
                attributes=dict(
                    standard_name=SPL_STANDARD_NAME, units="dB",
                    reference=("1 micropascal^2 per hertz" if label == "PSD_1h"
                               else "1 micropascal"),
                    cell_methods="time: median (interval: 1 s)",
                    coverage_content_type="physicalMeasurement",
                    source_product=label, source_file=GCS + key,
                    doi=f"https://doi.org/{source_doi(f)}",
                    source_title=as_text(f.attrs["title"]),
                    source_history=source_history(f),
                    source_citation=as_text(f.attrs["citation"]),
                    source_date_issued=as_text(f.attrs["date_issued"]),
                    comment=comment))
    if sl is not None:
        sl.attrs["time_coverage_resolution"] = "P1H"

    rows, methods, dois = [], {}, {}
    for key in product_keys("detections", site, dep):
        label = product_label(key, dep)
        with open_netcdf(key) as f:
            methods[label] = source_history(f)
            dois[label] = source_doi(f)
            # Variable names do not follow the label: `finwhale_1d` has
            # `finwhale_presence`, `Explosions` has `explosions_presence`, `sonar`
            # has `mfa_presence`. Match on the suffix, and demand exactly one.
            pres, eff = [[n for n in f if n.endswith(f"_{kind}")]
                         for kind in ("presence", "effort")]
            if len(pres) != 1 or len(eff) > 1:
                raise KeyError(f"{key}: expected one *_presence and at most one "
                               f"*_effort variable, found {pres + eff}")
            pres = np.asarray(f[pres[0]][()])
            eff = np.asarray(f[eff[0]][()]) if eff else np.full(len(pres), -1)
            # 7 of 8 files carry an all-zero phony `time`; the real times are in
            # start_time/end_time as milliseconds since epoch.
            if "start_time" in f:
                s = np.asarray(f["start_time"][()]).astype("int64")
                e = np.asarray(f["end_time"][()]).astype("int64")
            else:
                s = e = np.asarray(f["time"][()]).astype("int64")
        rows += [(s[i] * 10**6, e[i] * 10**6, label, int(pres[i]), int(eff[i]))
                 for i in range(len(s))]
    rows.sort()
    start, end, label, presence, effort = zip(*rows)

    det = group.require_group("detections")
    d = ("detection",)
    write_small_array(det, "start_time", np.array(start, "int64"), d, standard_name="time", **EPOCH)
    write_small_array(det, "end_time", np.array(end, "int64"), d,
         long_name="end of the detection window", **EPOCH)
    write_small_array(det, "label", np.array(label, "<U32"), d, long_name="detection label")
    write_small_array(det, "presence", np.array(presence, "int32"), d,
         long_name="presence flag as published",
         comment="1 = present, 0 = not present. Labels with a window suffix (_1d, "
                 "_1h, _1min) have one row per window, present or not; every other "
                 "label lists presences only.")
    write_small_array(det, "effort", np.array(effort, "int32"), d,
         long_name="detection effort flag as published",
         comment="1 = the window was analysed, 0 = it was not. Published only for "
                 "the daily (_1d) labels; -1 where the source has none.")

    labels = sorted(methods)
    lg = det.require_group("labels")
    lg.attrs.update(
        title="Detection labels",
        comment="One row per label in detections/label: what the label is, the "
                "detector that produced it, and the DOI of its published product.")
    d = ("label",)
    write_small_array(lg, "label", np.array(labels, "<U32"), d, long_name="detection label")
    sources = [sound_source(l) for l in labels]
    write_small_array(lg, "biological_taxon_name", np.array([t for _, t, _ in sources], "<U64"), d,
         standard_name="biological_taxon_name")
    write_small_array(lg, "biological_taxon_lsid", np.array([i for _, _, i in sources], "<U96"), d,
         standard_name="biological_taxon_lsid")
    write_small_array(lg, "source_type", np.array([k for k, _, _ in sources], "<U16"), d,
         long_name="biological, anthropogenic or unknown sound source")
    # The method is a property of the detector, not of a detection, and runs to
    # ~1 KB, so it lives per label.
    write_small_array(lg, "method", np.array([methods[l] for l in labels]), d,
         long_name="detection method as described by the source file",
         comment="Copied verbatim from the source netCDF's `history` attribute. "
                 "Automatic detectors and manual analyst logging are both "
                 "represented; read it before comparing labels.")
    write_small_array(lg, "doi", np.array([dois[l] for l in labels], "<U18"), d,
         long_name="DOI of the published detection product for this label",
         comment="Each detection product is archived separately, with its own DOI.")
    det.attrs.update(featureType="point", comment=(
        "One event table across all published detection files. Seven of eight carry "
        "an all-zero phony `time`; real times come from start_time/end_time "
        "(milliseconds since epoch). `<label>_presence` is not a CF standard name "
        "and is kept as long_name only. A row is a detection only where presence "
        "is 1."))
    return len(rows), len(labels)



BAND_HZ = (20.0, 24000.0)


def measure_counts_offset(group, n_hours: int = 8, block_s: int = 60):
    """dB to add to 20*log10(counts) for dB re 1 uPa, matched to NOAA's own BB_1h.

    NOAA publishes no counts-to-pressure conversion: the certificate gives the
    hydrophone sensitivity in volts, not the ADC full scale or gain. So replicate
    the BB_1h method stated in its `history` on the store's own audio (1 Hz / 1 s
    PSD, integrated over 20 Hz to 24 kHz, median of the 1-second values in the
    hour) and take the difference from the published hourly level.

    The median matters. On CI01_01 it recovers the offset to within 0.003 dB in
    every hour measured; a mean of the same 1-second powers is 1 to 4.5 dB off,
    and by a different amount each hour.

    Hours are spread evenly across the deployment and must lie wholly inside one
    recording. Each hour is read once, ~260 MB of FLAC and 691 MB of int16.
    """
    from scipy import signal

    w = group["audio/waveform"]
    sr = int(w.attrs["sample_rate_hz"])
    seg = group[w.attrs["time_index"]]
    ss, ns = seg["start_sample"][:], seg["n_samples"][:]
    st = seg["start_time"][:].astype("datetime64[ns]")
    bt = group["sound_levels/time"][:].astype("datetime64[ns]")
    bb = group["sound_levels/broadband_spl"][:]

    usable = []
    for h, t in enumerate(bt):
        k = int(np.searchsorted(st, t, side="right") - 1)
        if k < 0:
            continue
        off = int((t - st[k]) / np.timedelta64(1, "s") * sr)
        if off >= 0 and off + 3600 * sr <= ns[k]:
            usable.append((h, int(ss[k]) + off))
    picks = [usable[i] for i in np.linspace(0, len(usable) - 1, n_hours).astype(int)]

    rows = []
    with Progress("measuring calibration offset", len(picks), unit="hours",
                  every=30) as progress:
        for h, i0 in picks:
            hour = w[i0: i0 + 3600 * sr]
            power = []
            for b in range(0, 3600, block_s):
                x = hour[b * sr: (b + block_s) * sr].astype("float64")
                f, p = signal.welch(x.reshape(block_s, sr), fs=sr, nperseg=sr,
                                    window="hann", axis=-1)
                band = (f >= BAND_HZ[0]) & (f <= BAND_HZ[1])
                power.append(p[:, band].sum(-1) * (f[1] - f[0]))
            power = np.concatenate(power)
            rows.append(dict(time=str(bt[h]), broadband_spl=float(bb[h]),
                             offset_median=float(bb[h] - 10 * np.log10(np.median(power))),
                             offset_mean=float(bb[h] - 10 * np.log10(np.mean(power)))))
            progress.advance()
    med = np.array([r["offset_median"] for r in rows])
    return dict(offset_db=float(med.mean()), standard_deviation_db=float(med.std(ddof=1)),
                hours=rows)


#: The level of a full-scale int16 sample, in dB re one count.
INT16_FULL_SCALE_DB = 20 * np.log10(32768)

#: The test setup every certificate in the archive names.
CALIBRATION_SETUP = dict(frequency_hz=250.0, source_model="Center 327",
                         source_serial="130307390", coupler="OIC1",
                         reference_instrument="B&K 2236 s/n 2015497", operator="JA",
                         source="certificate")


@dataclass(frozen=True)
class HydrophoneSensitivity:
    """A SoundTrap HP1 certificate: the hydrophone's sensitivity in volts."""

    sensitivity_db: float
    device_serial: str
    test_date: str
    issuer = "Ocean Instruments NZ"
    summary = ("The certificate gives the hydrophone sensitivity in volts, so it does not "
               "by itself convert int16 counts to micropascals: the ADC full-scale voltage "
               "and the gain setting are absent from it.")

    def write(self, cal) -> None:
        write_small_array(cal, "hydrophone_sensitivity", np.float64(self.sensitivity_db), (),
                          long_name="hydrophone sensitivity", units="dB re 1 uPa/V",
                          **CALIBRATION_SETUP, instrument_model="SoundTrap HP1",
                          source_level_db_re_1upa=120.0, device_serial=self.device_serial,
                          test_date=self.test_date)

    def cross_check(self, offset_db: float) -> str:
        at_1v = self.sensitivity_db - INT16_FULL_SCALE_DB
        return (f"If the ADC full scale were 1 V, the certificate's {self.sensitivity_db} dB "
                f"re 1 uPa/V would give {at_1v:.1f} dB. This value is "
                f"{at_1v - offset_db:.1f} dB lower, which a full scale of "
                f"{10 ** ((offset_db - at_1v) / 20):.2f} V would explain.")


@dataclass(frozen=True)
class FullScaleResponse:
    """A SoundTrap 300 certificate: the recorder's end-to-end response at digital full
    scale for each gain, which converts counts to micropascals directly."""

    response_db: dict[str, float]
    calibration_tone_db: float
    device_serial: str
    test_date: str
    issuer = "Ocean Instruments NZ"

    def implied_offsets(self) -> dict[str, float]:
        return {gain: level - INT16_FULL_SCALE_DB for gain, level in self.response_db.items()}

    @property
    def summary(self) -> str:
        return ("The certificate gives the recorder's end-to-end response at digital full "
                "scale for each gain, which converts counts to micropascals directly: "
                + ", ".join(f"{v:.1f} dB at {g} gain" for g, v in self.implied_offsets().items())
                + ". It does not say which gain the deployment used.")

    def write(self, cal) -> None:
        attrs = dict(CALIBRATION_SETUP, instrument_model="SoundTrap 300 STD",
                     source_level_db_re_1upa=121.0, device_serial=self.device_serial,
                     test_date=self.test_date)
        for gain, level in self.response_db.items():
            write_small_array(cal, f"full_scale_response_{gain}_gain", np.float64(level), (),
                              long_name=f"sound pressure at digital full scale, {gain} gain",
                              units="dB re 1 uPa", **attrs)
        write_small_array(cal, "calibration_tone_level", np.float64(self.calibration_tone_db), (),
                          long_name="level of the recorder's calibration tone, referred to input",
                          units="dB re 1 uPa", tone_frequency_hz=1000.0, **attrs)

    def cross_check(self, offset_db: float) -> str:
        implied = self.implied_offsets()
        nearest = min(implied, key=lambda gain: abs(offset_db - implied[gain]))
        return ("The certificate's full-scale responses imply "
                + " and ".join(f"{v:.1f} dB at {g} gain" for g, v in implied.items())
                + f"; this measured value is {abs(offset_db - implied[nearest]):.2f} dB "
                f"from the {nearest}-gain figure.")


# Transcribed by hand from each one-page PDF, which has no text layer to parse, and
# keyed by the PDF's sha256 so a transcription is only ever applied to its own file.
CERTIFICATES = {
    "f3f378411d23bcb7def863683964a469da1ceb6fe187e0d42505b94cda7a785d":  # CI01_01
        HydrophoneSensitivity(177.4, "1044", "2018-08-31"),
    "c506afa269534f6c07727a35a771d56db6f2ede7cd61e5bb66d13843a4824972":  # CI01_02
        HydrophoneSensitivity(177.1, "1054", "2018-09-19"),
    "aca59f27c9d2151ae64fb14f62438f7b71301f1f6cc1b828499122b89f7a47e2":  # CI02_01
        HydrophoneSensitivity(178.4, "1046", "2018-08-31"),
    "3e687604f0bfbfb3312045a4e0dccd7d706e49fbfae273cb26d4ea18084836ae":  # CI04_01
        HydrophoneSensitivity(177.5, "1043", "2018-08-31"),
    "aad2e719ba9ae56bc3f5214ea92b45fa45b435deeada3411f3c569c0d2234d5d":  # CI05_01
        HydrophoneSensitivity(178.0, "1035", "2018-08-21"),
    "c2dd8bdb4bbd1ce02ba613e57a200dfafad32ba59d2837aaffd8f6ff94d156df":  # OC01_01
        HydrophoneSensitivity(178.2, "1057", "2018-09-24"),
    "d1db20b76f3b4f3f74701909218f9a8a4ac7aec79a34522ab92019749d396df7":  # OC01_03
        HydrophoneSensitivity(176.5, "1065", "2018-09-24"),
    "4994600e41ed813d46d31c240ea84ba37e8bfd9f5e0c3056de641c0a5ef885a7":  # SB01_03
        FullScaleResponse({"high": 171.8, "low": 184.1}, 134.8, "1677778970", "2016-05-11"),
}


def write_calibration(group, site: str, dep: str, measured: dict | None):
    """The certificate byte for byte, the sensitivity it states, and the offset
    `measure_counts_offset` derived. `measured` is that function's return value, or
    None for a deployment with no published sound levels to measure against."""
    ((key, _),) = list_keys(deployment_prefix(site, dep) + "ancillary/calibration/", ".pdf")
    pdf = read_object(key)
    sha = hashlib.sha256(pdf).hexdigest()
    if sha not in CERTIFICATES:
        raise KeyError(f"no transcription for {key} (sha256 {sha}); add it to "
                       f"CERTIFICATES")
    certificate = CERTIFICATES[sha]

    cal = group.require_group("calibration")
    write_small_array(cal, "certificate", np.frombuffer(pdf, dtype="uint8"), ("byte",),
         long_name="calibration certificate, verbatim", media_type="application/pdf",
         source_uri=BASE + key, sha256=sha, issuer=certificate.issuer,
         test_date=certificate.test_date,
         comment="The bytes of the published PDF, stored as a plain uint8 array. Zarr "
                 "has no PDF codec and no reader will open this for you: media_type "
                 "is a hint, not a decoder. To recover the file: "
                 "open('calibration.pdf', 'wb').write(bytes(group['certificate'][:]))")
    certificate.write(cal)

    if measured is not None:
        offset_db = measured["offset_db"]
        write_small_array(cal, "counts_to_upa_db", np.float64(round(offset_db, 3)), (),
             long_name="offset from int16 counts to sound pressure", units="dB",
             comment="Add this to 20*log10(|counts|) to obtain dB re 1 uPa. Derived, not "
                     "published: NOAA's BB_1h method, as its history states it (1 Hz / 1 s "
                     "PSD, integrated over 20 Hz to 24 kHz, hourly median of the 1-second "
                     "values), applied to audio/waveform and differenced against "
                     "sound_levels/broadband_spl for the same hour.",
             method=f"{len(measured['hours'])} full hours spread evenly across the "
                    f"deployment, each wholly inside one recording",
             hours=[h["time"] for h in measured["hours"]],
             standard_deviation_db=round(measured["standard_deviation_db"], 4),
             n_hours=len(measured["hours"]), band_hz=list(BAND_HZ),
             source="derived from sound_levels/broadband_spl",
             cross_check=certificate.cross_check(offset_db))
        offset = ("counts_to_upa_db is measured against this deployment's own published "
                  "sound levels, and cross_check compares it with the certificate.")
    elif "sound_levels" in group:
        offset = ("The counts-to-micropascal offset was not measured for this build, and "
                  "this group holds none.")
    else:
        offset = ("NOAA published no sound levels for this deployment, so no "
                  "counts-to-micropascal offset could be measured against them, and this "
                  "group holds none.")

    serial = certificate.device_serial
    serial_recordings = str(group.attrs["instrument_serial_number"])
    cal.attrs.update(
        title=f"Hydrophone calibration for deployment {dep}",
        comment=f"NOAA publishes this calibration as a one-page PDF beside the recordings. "
                f"{certificate.summary} {offset}",
        instrument_serial_number_certificate=serial,
        instrument_serial_number_recordings=serial_recordings,
        serial_number_comment=(
            f"The certificate and every audio file name both name device serial {serial}."
            if serial == serial_recordings else
            f"The certificate names device serial {serial}; every audio file name and the "
            f"deployment attributes name {serial_recordings}. Both are recorded verbatim, "
            f"with no claim about which identifies which component."))


# Site-code prefixes, from the project overview (LAYOUTS.md, "The archive").
SANCTUARY = {"CI": "Channel Islands", "MB": "Monterey Bay", "OC": "Olympic Coast",
             "SB": "Stellwagen Bank", "GR": "Gray's Reef", "FK": "Florida Keys",
             "HI": "Hawaiian Islands Humpback Whale", "PM": "Papahānaumokuākea"}

ROOT_DESCRIPTION = """\
## SanctSound, virtualized

Hydrophone audio from [SanctSound](https://sanctsound.ioos.us/), one group per deployment:

%(deployments)s

The audio stays in NOAA's public bucket: this store holds byte-range references into the
FLAC files under `gs://noaa-passive-bioacoustic`, and copies none of them. NOAA's
detections and hourly sound levels for each deployment, where NOAA published them, sit
beside its audio and share its clock.

**Reading the audio needs two opt-ins.** Chunks end where recordings end, so the grid is
rectilinear: use zarr-python 3.2 or newer and set
`zarr.config.set({"array.rectilinear_chunks": True})`. Zarr also registers no audio
codec, so the chunks need one: install tag `zarr3-flac-codec-2026-10-19` of
[flac-numcodecs](%(codec_url)s), which registers a `flac` codec with Zarr. It is not in a
release yet. xarray opens the store from release 2026.09.0, the first with
[pydata/xarray#11592](https://github.com/pydata/xarray/pull/11592). The published
products read with stock Zarr.

**Published sources, each with its own DOI**

- Raw audio: [%(audio_doi)s](https://doi.org/%(audio_doi)s)
- Detections: one DOI per label, in `<deployment>/detections/labels/doi`
- Sound levels: one DOI per product, on each variable in `<deployment>/sound_levels`

**License.** NOAA's terms, carried verbatim in the `license` attribute: the data may be
used and redistributed for free, but they are not intended for legal use, and NOAA gives
no warranty of accuracy or fitness.

Each deployment's `calibration` group holds the certificate NOAA publishes as a PDF, byte
for byte, and, where NOAA published sound levels, an offset from counts to micropascals
derived from them.
"""


def deployment_line(name: str, group) -> str:
    """One Markdown bullet describing a deployment, from its own attributes."""
    a, w = group.attrs, group["audio/waveform"]
    hours = w.shape[0] / w.attrs["sample_rate_hz"] / 3600
    return (f"- `{name}`: {SANCTUARY[a['site_id'][:2]]} site {a['site_id']}, "
            f"{str(a['time_coverage_start'])[:10]} to {str(a['time_coverage_end'])[:10]}, "
            f"{hours:,.0f} hours at {w.attrs['sample_rate_hz'] / 1000:g} kHz")


ROOT_INHERITED = ("naming_authority", "license", "acknowledgement", "keywords",
                  "keywords_vocabulary", "project", "publisher_name", "publisher_email",
                  "publisher_url", "institution", "contributor_name", "contributor_role")


def write_provenance(root, group, site: str, dep: str):
    """Root and deployment attributes, inherited from NOAA's metadata where it says it.

    The deployment's own metadata JSON supplies location, depth, DOI and people; a
    product file (BB_1h where published, else a detection file) supplies the
    project-level ACDD attributes, which every product file repeats. The root
    attributes are replaced whole, keeping only date_created.
    """
    prefix = deployment_prefix(site, dep)
    meta_key = f"{prefix}metadata/SanctSound_{dep}.json"
    meta = json.loads(read_object(meta_key))
    # Every product file repeats these attributes; not every deployment has sound
    # levels, but every one has detections.
    source_key = ([k for k in product_keys("sound_level_metrics", site, dep)
                   if k.endswith("_BB_1h.nc")] or product_keys("detections", site, dep))[0]
    with open_netcdf(source_key) as f:
        nc = {k: as_text(f.attrs[k]) for k in f.attrs if not k.startswith("_")}
    uri = str(group["audio/segments/source_uri"][0])
    serial = uri.split("/")[-1].split("_")[3]
    lat, lon = float(meta["LOCATION"]["lat"]), float(meta["LOCATION"]["lon"])

    quality = "; ".join(
        f"{q['Date Range']}, {q['Frequency (Hz)']} Hz: {q['Quality']}"
        + (f" ({q['Details']})" if q.get("Details") else "")
        for q in meta["DATA_QUALITY"].values())
    group.attrs.update(
        deployment_id=dep, site_id=site, sanctuary=SANCTUARY[site[:2]],
        platform=meta["PLATFORM_NAME"],
        instrument=nc["instrument"], instrument_serial_number=serial,
        geospatial_lat_min=lat, geospatial_lat_max=lat,
        geospatial_lon_min=lon, geospatial_lon_max=lon,
        calibration_url=f"{GCS}{prefix}ancillary/calibration/",
        license=nc["license"],
        license_source="verbatim from the SanctSound product netCDF files",
        citation=meta["CITATION"], doi=meta["DOI"],
        project="SanctSound, a NOAA Office of National Marine Sanctuaries and U.S. "
                "Navy collaboration",
        source_organization=meta["SOURCE_ORGANIZATION"].replace(",", ", "),
        funding_organization=meta["FUNDING_ORGANIZATION"].replace(",", ", "),
        chief_scientist=meta["CHIEF_SCIENTIST"],
        doi_comment="Every published product carries its own DOI. The DOI above is "
                    "the raw audio. The detections and the sound levels carry theirs "
                    "on the variable they came from.",
        sensor_depth_m=meta["SENSOR_DEPTH"], bottom_depth_m=meta["BOTTOM_DEPTH"],
        geospatial_vertical_min=meta["BOTTOM_DEPTH"],
        geospatial_vertical_max=meta["SENSOR_DEPTH"],
        geospatial_vertical_units="m", geospatial_vertical_positive="up",
        depth_source=meta_key,
        data_quality=quality,
        data_quality_comment=nc["comment"],
        data_quality_source="data_quality from the deployment metadata JSON; "
                            "data_quality_comment from the product netCDF files")

    now = datetime.now(timezone.utc)
    root.attrs.put({k: nc[k] for k in ROOT_INHERITED} | dict(
        date_created=root.attrs.get("date_created", now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        id=meta["DOI"], citation=meta["CITATION"],
        id_comment="id is the DOI of the raw audio, which is most of what this store "
                   "addresses. Every derived product carries its own DOI, on the "
                   "variable or label it became.",
        title="NOAA-Navy SanctSound — virtualized",
        summary="SanctSound passive acoustic data, virtualized. Each deployment group "
                "holds NOAA's hydrophone audio as byte-range references into the "
                "public FLAC files in gs://noaa-passive-bioacoustic, with no copy of "
                "the audio, on a single sample axis beside the detections and the "
                "hourly sound levels that NOAA publishes for the same deployment. "
                "Reading the audio needs two opt-ins: a rectilinear chunk grid, "
                "because chunks end where recordings end, and a FLAC codec, because "
                "Zarr registers none. The published products are copied, not "
                "referenced. NOAA's own title, history and citation for each product "
                "are kept verbatim on the variable it became.",
        description=ROOT_DESCRIPTION % dict(
            deployments="\n".join(deployment_line(n, g) for n, g in sorted(root.groups())),
            codec_url=CODEC_URL, audio_doi=meta["DOI"].split("doi.org/")[-1]),
        comment="One group per deployment. Audio arrays are byte-range references "
                "into NOAA's bucket; nothing is copied. Sample rate and FLAC blocksize "
                "vary between deployments, so each deployment carries its own codec "
                "config. Reading audio needs the 'flac' Zarr codec from flac-numcodecs "
                f"({CODEC_URL}); the products are materialized and read with stock zarr.",
        creator_name="Earthmover", program="NOAA Big Data Program",
        source="gs://noaa-passive-bioacoustic/sanctsound/",
        Conventions="CF-1.8, ACDD-1.3",
        standard_name_vocabulary="CF Standard Name Table v94",
        date_modified=now.strftime("%Y-%m-%d")))



def selftest_from_store_only(root: zarr.Group) -> list[dict]:
    """Resolve each detection time to an audio sample using only the store.

    Nothing is passed in: not the sample rate, not the scan. If this passes, a
    stranger holding the repo can do the same, which is the bar for calling the
    store self-describing. A detection resolves when its start time falls inside a
    recording; recordings do not tile the timeline, so one that falls in a gap
    between them, or before the first, is refused.
    """
    return [selftest_deployment(name, root[name]) for name in sorted(root.group_keys())]


def selftest_deployment(dep: str, group) -> dict:
    seg = group["audio/segments"]
    sample_rate = int(group["audio/waveform"].attrs["sample_rate_hz"])
    starts = seg["start_time"][:].astype("datetime64[ns]")
    n_samples = seg["n_samples"][:]
    times = group["detections/start_time"][:].astype("datetime64[ns]")
    k = np.searchsorted(starts, times, side="right") - 1
    into = np.round((times - starts[np.maximum(k, 0)]) / np.timedelta64(1, "s")
                    * sample_rate)
    resolved = int(np.count_nonzero((k >= 0) & (into < n_samples[np.maximum(k, 0)])))
    return dict(deployment=dep, segments=len(starts), sample_rate=sample_rate,
                detections=len(times), resolved=resolved, refused=len(times) - resolved)



def repository(location: str, *, create: bool = False):
    """The Icechunk repo at ``location``: a directory, or ``arraylake://<org>/<repo>``.

    A local repo gets a virtual chunk container for NOAA's bucket. Arraylake repos
    hold their own containers, and this never changes them.
    """
    from minus80.core.icechunk_io import (ARRAYLAKE, arraylake_repository, create_repo,
                                          open_repository)

    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[BASE])
    if create:
        return create_repo(location, [], prefixes=[BASE], open_existing=True)
    return open_repository(location)


def build(dep: str, table: DeploymentTable, repo_location: str, *, branch: str = "main",
          measure: bool = True) -> None:
    """Write one deployment into a repo and commit it.

    ``measure=False`` skips the calibration offset, which reads eight hours of
    audio, and leaves the group without ``counts_to_upa_db``.
    """
    # The waveform's chunk grid is rectilinear, and zarr refuses to read or write
    # one unless this is set.
    with zarr.config.set({"array.rectilinear_chunks": True}):
        dep, site = dep.upper(), dep.upper()[:4]
        repo = repository(repo_location, create=True)
        session = repo.writable_session(branch)
        root = zarr.open_group(session.store, mode="a")
        dg = root.require_group(dep)
        sample_rate = int(table.chunks.sample_rate)
        hours = int(table.chunks.n_samples.sum()) / sample_rate / 3600

        write_audio(dg, table)
        print(f"waveform: {len(table.chunks.edge)} chunks, {hours:.1f} h at {sample_rate} Hz, "
              f"blocksize {int(table.chunks.blocksize)}", flush=True)
        n_events, n_labels = write_products(dg, site, dep)
        print(f"products: {n_events} detections, {n_labels} labels", flush=True)
        n_temp = write_temperature(dg, site, dep)
        print(f"temperature: {n_temp:,} samples" if n_temp
              else "temperature: NOAA published no logger record", flush=True)
        measured = None
        if "sound_levels" in dg and measure:
            measured = measure_counts_offset(dg)
            print(f"calibration: {measured['offset_db']:.3f} dB, "
                  f"sd {measured['standard_deviation_db']:.4f} dB", flush=True)
        # write_calibration reads the serial number write_provenance records
        write_provenance(root, dg, site, dep)
        write_calibration(dg, site, dep, measured)
        snapshot = session.commit(f"{dep}: {len(table.chunks.n_samples)} recordings "
                                  f"({hours:.1f} h)")
        print(f"committed {snapshot}", flush=True)
        check = zarr.open_group(repo.readonly_session(branch).store, mode="r")
        for row in selftest_from_store_only(check):
            print(row, flush=True)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="scan recordings into a chunk table")
    s.add_argument("deployment")
    s.add_argument("windows", nargs="*", help="START/END, ISO 8601, UTC")
    s.add_argument("--cache", type=Path, required=True,
                   help="directory for per-recording scans, so a run resumes")
    s.add_argument("--out", type=Path, required=True, help="chunk table .npz")
    b = sub.add_parser("build", help="write a deployment into a repo from a chunk table")
    b.add_argument("deployment")
    b.add_argument("--table", type=Path, required=True, help="chunk table .npz")
    b.add_argument("--repo", required=True,
                   help="a directory, or arraylake://<org>/<repo>")
    b.add_argument("--branch", default="main")
    b.add_argument("--skip-calibration-measure", action="store_true")
    args = ap.parse_args(argv)
    if args.command == "scan":
        scan_deployment(args.deployment, args.windows, args.cache, args.out)
    else:
        build(args.deployment, DeploymentTable.load(args.table), args.repo,
              branch=args.branch, measure=not args.skip_calibration_measure)


if __name__ == "__main__":
    main()
