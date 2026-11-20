"""Draw the whole Brain-Wide Map archive as one tall figure, ``diagrams/archive.svg``.

The figure is a tour of everything the International Brain Laboratory
publishes for the Brain-Wide Map, not only what the store holds: DANDI
000409's NWB files and videos, then every product in the IBL public bucket,
then the release history. Each product gets a panel (what it is, where, its
format, file count, size, license, and whether the store holds it) and, where
one helps, small pictures drawn from real data of the trial session.

Where the values come from:

- DANDI's asset list (``sources.dandi_assets``) for the dandiset's files;
- IBL's ONE caches: ``Brainwidemap`` for the release's sessions and files,
  and the bucket-wide cache for everything public in ``data/``;
- bucket listings for the prefixes outside ``data/``;
- the trial store for the NWB and spike-sorting pictures, and small byte
  ranges of the sources for the rest: one video frame, one cluster's
  waveform, the atlas thumbnail volume, and two columns of each release's
  cluster table.

Pictures are cached under ``minus80.core.fetch.cache_dir()``, so drawing
again reads nothing from the hosts. LAYOUTS.md explains each part; the
figure carries short labels only.
"""

from __future__ import annotations

import collections
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import obstore
import zarr
from sources import (
    DANDISET,
    DANDI_VERSION,
    IBL_BUCKET,
    dandi_assets,

    dandi_json,
    npy_header,
    one_cache,
)

from minus80 import diagrams as d
from minus80.core.fetch import cache_dir, read_range
from minus80.diagrams import ACCENTS, Canvas, Row

EID = "465c44bd-2e67-4112-977b-36e1ac7e3f8c"
SESSION = f"sub-ZM-2241/ses-{EID}"
SESSION_PATH = "mainenlab/Subjects/ZM_2241/2020-01-28/001"
LICENSE = "CC BY 4.0"

WIDTH = 1530
PANEL_X, PANEL_WIDTH = 24, 640
PICTURE_X, PICTURE_WIDTH, PICTURE_GAP = 690, 250, 22

PICTURES = Path(cache_dir()) / "ibl-archive-pictures"

#: The release notes are the only record of what each release changed.
RELEASE_NOTES = "docs.internationalbrainlab.org/notebooks_external/2025_data_release_brainwidemap.html"
RELEASES = [
    ("2022 Q4", "first release: 354 sessions, 547 probes; spike sorting in pykilosort/"),
    ("2024-02-15", "105 sessions added; 285 video timestamps corrected, 139 videos removed"),
    ("2024 (05-06)", "whole dataset re-sorted; files in pykilosort/#2024-05-06#/; 621,733 units"),
    ("2025 Q1", "wheel polarity fixed in 62 sessions; audio sync patched"),
    ("2025 Q3", "passive intervals, Lightning Pose and saturation intervals added"),
    ("2025 Q4", "passive Gabor timing fixed (off by one trial)"),
    ("2026-03-09", "DANDI 000409 version 0.260309.1324 published"),
]


@dataclass
class Part:
    """One product of the archive: its panel's facts and the pictures drawn beside it."""

    title: str
    what: str
    where: str
    format: str
    files: str
    size: str
    store: str
    held: bool
    pictures: list[tuple[str, bytes, str]] = field(default_factory=list)


def cached(name: str, make) -> bytes:
    """A picture's bytes, made once and kept under :data:`PICTURES`."""
    path = PICTURES / name
    if not path.exists():
        PICTURES.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make())
    return path.read_bytes()


def size(n: float) -> str:
    return d.size_text(int(n))


def store_group(repo, path: str, **ref) -> zarr.Group:
    return zarr.open_group(repo.readonly_session(**(ref or {"branch": "main"})).store, path=path, mode="r")


def ap_picture(repo) -> bytes:
    """A third of a second of every AP channel, channels down, time across."""
    data = store_group(repo, f"{SESSION}/raw/acquisition/ElectricalSeriesProbe00AP")["data"]
    start = 13020 * 2000
    samples = data[start:start + 9000, :].astype("float32")
    samples -= np.median(samples, axis=0)
    # Every other channel and every 36th sample: 192 rows by 250 columns, the picture's own size.
    return d.thumbnail(samples.T[::-2, ::36], 250, contrast=(2, 98))


def lf_picture(repo) -> bytes:
    """Five seconds of the LF band on six channels spread along the probe."""
    data = store_group(repo, f"{SESSION}/raw/acquisition/ElectricalSeriesProbe00LF")["data"]
    samples = data[13020 * 100:13020 * 100 + 12500, ::64].astype("float32")
    samples = (samples - samples.mean(axis=0)) / samples.std(axis=0)

    def draw(axes):
        for i in range(samples.shape[1]):
            axes.plot(samples[:, i] + i * 6, linewidth=0.5, color="#2a78d6")
    return d.plot(draw, width=250, height=110)


def wheel_picture(repo) -> bytes:
    """The wheel's position over the first two minutes of the task."""
    wheel = store_group(repo, f"{SESSION}/processed/processing/wheel/WheelPosition")
    times, position = wheel["timestamps"][:], wheel["data"][:]
    keep = times < times[0] + 120

    def draw(axes):
        axes.plot(times[keep], position[keep], linewidth=0.7, color="#1baf7a")
    return d.plot(draw, width=250, height=90)


def pupil_picture(repo) -> bytes:
    """The left pupil's smoothed diameter over the first two minutes."""
    pupil = store_group(repo, f"{SESSION}/processed/processing/pupil/LeftPupilDiameterSmoothed")
    times, size_ = pupil["timestamps"][:], pupil["data"][:]
    keep = times < times[0] + 120

    def draw(axes):
        axes.plot(times[keep], size_[keep], linewidth=0.7, color="#7c3aed")
    return d.plot(draw, width=250, height=90)


def trials_picture(repo) -> bytes:
    """Each trial's signed contrast, colored by whether the mouse was rewarded."""
    trials = store_group(repo, f"{SESSION}/processed/intervals/trials")
    contrast = trials["gabor_stimulus_contrast"][:]
    side = np.where(trials["gabor_stimulus_side"][:] == "left", -1, 1)
    rewarded = trials["is_mouse_rewarded"][:]

    def draw(axes):
        n = np.arange(len(contrast))
        axes.scatter(n, side * contrast, s=3, c=np.where(rewarded, "#1baf7a", "#eb6834"))
        axes.axhline(0, color="#cbd5e1", linewidth=0.5)
    return d.plot(draw, width=250, height=110)


def raster_picture(repo, tag: str) -> bytes:
    """Twenty seconds of spikes, time across and depth on the probe up."""
    spikes = store_group(repo, f"{SESSION}/alf/probe00/pykilosort/spikes", tag=tag)
    rows = spikes["times"].chunks[0]
    times, depths = spikes["times"][:rows], spikes["depths"][:rows]
    keep = (times > times[0] + 5) & (times < times[0] + 25)

    def draw(axes):
        axes.scatter(times[keep], depths[keep], s=0.02, alpha=0.4, linewidths=0, color="#0f172a")
        axes.set_ylim(0, 3840)
    return d.plot(draw, width=250, height=130)


def waveform_picture(repo) -> bytes:
    """The mean waveform of the largest cluster on its eight nearest channels."""
    clusters = store_group(repo, f"{SESSION}/alf/probe00/pykilosort/clusters", tag="sort-2024")
    biggest = int(np.argmax(clusters["amps"][:]))
    waveforms = clusters["waveforms"]
    url = waveforms.attrs["source_url"]
    shape, dtype, offset = npy_header(url)
    one = int(np.prod(shape[1:])) * dtype.itemsize
    wave = np.frombuffer(read_range(url, offset + biggest * one, offset + (biggest + 1) * one),
                         dtype=dtype).reshape(shape[1:])

    def draw(axes):
        step = np.ptp(wave[:, :8]) * 0.8
        for i in range(8):
            axes.plot(wave[:, i] - i * step, linewidth=0.9, color="#eb6834")
    return d.plot(draw, width=250, height=150)


def video_frame(url: str) -> bytes:
    """One frame, a minute into the video, read with PyAV's range requests."""
    import av

    with av.open(url) as container:
        container.seek(60 * 1_000_000)
        frame = next(container.decode(video=0)).to_ndarray(format="gray")
    return d.thumbnail(frame, 250, quality=60)


def atlas_picture(repo) -> bytes:
    """A sagittal slice of IBL's 200 µm Allen template, with the probe's channels on it.

    The template is ``(ml, dv, ap)`` at 200 µm; the channels' Allen CCF
    positions, in µm, come from the raw NWB file's electrode localization.
    """
    url = f"s3://{IBL_BUCKET}/atlas/agea/image.npy"
    shape, dtype, offset = npy_header(url)
    volume = np.frombuffer(read_range(url, offset, offset + int(np.prod(shape)) * dtype.itemsize),
                           dtype=dtype).reshape(shape)
    ccf = store_group(repo, f"{SESSION}/raw/general/localization/ElectrodesCCFv3")
    probe = ccf["probe_name"][:] == "Probe00"
    ap, dv, ml = ccf["x"][:][probe] / 200, ccf["y"][:][probe] / 200, ccf["z"][:][probe] / 200
    slice_ = volume[int(round(np.median(ml)))]

    def draw(axes):
        axes.imshow(slice_, cmap="gray", aspect="equal")
        axes.plot(ap, dv, ".", markersize=1.5, color="#eb6834")
    return d.plot(draw, width=250, height=160)


def release_units_picture() -> bytes:
    """Units and good units in each release's cluster table, read two columns at a time."""
    import pyarrow.fs
    import pyarrow.parquet as pq

    fs = pyarrow.fs.S3FileSystem(anonymous=True, region="us-east-1")
    releases = ["2022_Q4_IBL_et_al_BWM", "2023_Q4_IBL_et_al_BWM_2", "2024_Q2_IBL_et_al_BWM"]
    counts = []
    for release in releases:
        label = pq.read_table(f"{IBL_BUCKET}/aggregates/{release}/clusters.pqt", filesystem=fs,
                              columns=["label"]).column("label").to_numpy()
        counts.append((len(label), int((label == 1).sum())))

    def draw(axes):
        x = np.arange(len(counts))
        axes.bar(x - 0.2, [c[0] for c in counts], width=0.4, color="#cbd5e1")
        axes.bar(x + 0.2, [c[1] for c in counts], width=0.4, color="#2a78d6")
        for i, (n, good) in enumerate(counts):
            axes.text(i, n, f"{n:,} / {good:,}", ha="center", va="bottom", fontsize=7)
        axes.set_ylim(0, max(c[0] for c in counts) * 1.2)
    return d.plot(draw, width=250, height=110)


def dandi_video_url() -> str:
    asset = next(a for a in dandi_assets() if EID in a["path"] and a["path"].endswith("VideoLeftCamera.mp4"))
    meta = dandi_json(f"dandisets/{DANDISET}/versions/{DANDI_VERSION}/assets/{asset['asset_id']}/")
    return next(u for u in meta["contentUrl"] if ".s3.amazonaws.com/blobs/" in u)


def bucket_listing(prefix: str) -> tuple[int, int]:
    """File count and bytes under one prefix of the IBL bucket."""
    store = obstore.store.S3Store(IBL_BUCKET, region="us-east-1", skip_signature=True)
    files = sizes = 0
    for batch in obstore.list(store, prefix=prefix):
        files += len(batch)
        sizes += sum(o["size"] for o in batch)
    return files, sizes


def public_cache() -> dict:
    """File count, bytes, sessions and top-level folders of everything public in ``data/``.

    Read from IBL's bucket-wide ONE cache, a 180 MB zip, once.
    """
    summary = PICTURES / "public-cache-summary.json"
    if summary.exists():
        return json.loads(summary.read_text())
    import pandas as pd

    local = Path(cache_dir()) / "ibl-one-cache" / "all" / "cache.zip"
    if not local.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        store = obstore.store.S3Store(IBL_BUCKET, region="us-east-1", skip_signature=True)
        local.write_bytes(bytes(obstore.get(store, "caches/alyx/cache.zip").bytes()))
    with zipfile.ZipFile(local) as z:
        datasets = pd.read_parquet(io.BytesIO(z.read("datasets.pqt")), columns=["rel_path", "file_size"])
        sessions = pd.read_parquet(io.BytesIO(z.read("sessions.pqt")))
        date = json.loads(z.read("cache_info.json"))["date_created"]
    top = datasets.rel_path.str.split("/").str[0]
    folders = datasets.groupby(top).file_size.sum().sort_values(ascending=False)
    result = {"files": len(datasets), "bytes": int(datasets.file_size.sum()),
              "sessions": len(sessions), "projects": int(sessions.projects.astype(str).nunique()),
              "date": date, "folders": [[k, int(v)] for k, v in folders.head(6).items()]}
    PICTURES.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(result))
    return result


def release_collections() -> dict[str, tuple[int, int, int]]:
    """For the release's ONE cache: files, bytes and sessions per collection, probes merged."""
    datasets = one_cache("Brainwidemap")["datasets"].reset_index()
    collection = (datasets.rel_path.str.rsplit("/", n=1).str[0]
                  .str.replace(r"#[^#]+#", "#rev#", regex=True)
                  .str.replace(r"probe0\d[ab]?", "<probe>", regex=True))
    grouped = datasets.groupby(collection).agg(files=("file_size", "size"), bytes=("file_size", "sum"),
                                               sessions=("eid", "nunique"))
    return {k: (int(r.files), int(r.bytes), int(r.sessions)) for k, r in grouped.iterrows()}


def parts(repo) -> list[tuple[str, str, list[Part]]]:
    """The archive's products, grouped by host, each with its facts and pictures."""
    assets = dandi_assets()
    kinds = collections.defaultdict(lambda: [0, 0])
    for asset in assets:
        for suffix in ("desc-raw_ecephys.nwb", "desc-processed_behavior+ecephys.nwb", "Camera.mp4"):
            if asset["path"].endswith(suffix):
                kinds[suffix][0] += 1
                kinds[suffix][1] += asset["size"]

    release = release_collections()
    public = public_cache()

    def collection(name: str) -> tuple[str, str]:
        files, bytes_, sessions = release[name]
        return f"{files:,} in {sessions} sessions", size(bytes_)

    dandi = [
        Part("raw NWB", "raw voltage of both probes: AP band 30 kHz, LF band 2.5 kHz, sync",
             "dandiarchive/blobs (s3, us-east-2); ..._desc-raw_ecephys.nwb",
             "NWB 2.9 (HDF5), gzip, 10 MB chunks", f"{kinds['desc-raw_ecephys.nwb'][0]:,}",
             size(kinds["desc-raw_ecephys.nwb"][1]), "in store: {session}/raw", True,
             [("0.3 s of AP, 384 channels", cached("ap.jpg", lambda: ap_picture(repo)), "ElectricalSeriesProbe00AP"),
              ("5 s of LF, 6 channels", cached("lf.png", lambda: lf_picture(repo)), "ElectricalSeriesProbe00LF")]),
        Part("processed NWB", "trials, wheel, pupil, licks, pose, passive stimuli, 2024 units",
             "dandiarchive/blobs; ..._desc-processed_behavior+ecephys.nwb",
             "NWB 2.9 (HDF5)", f"{kinds['desc-processed_behavior+ecephys.nwb'][0]:,}",
             size(kinds["desc-processed_behavior+ecephys.nwb"][1]), "in store: {session}/processed", True,
             [("trials: contrast by side, rewarded", cached("trials.png", lambda: trials_picture(repo)), "intervals/trials"),
              ("wheel position, 2 min", cached("wheel.png", lambda: wheel_picture(repo)), "processing/wheel"),
              ("pupil diameter, 2 min", cached("pupil.png", lambda: pupil_picture(repo)), "processing/pupil")]),
        Part("camera videos", "left, right and body cameras of the mouse during the session",
             "dandiarchive/blobs; ..._Video{Left,Right,Body}Camera.mp4", "MP4, H.264, 60 or 150 Hz",
             f"{kinds['Camera.mp4'][0]:,}", size(kinds["Camera.mp4"][1]),
             "left out: video, not an array", False,
             [("left camera, 1 min in", cached("video.jpg", lambda: video_frame(dandi_video_url())),
               "VideoLeftCamera.mp4")]),
    ]
    files_2022 = collection("alf/<probe>/pykilosort")
    files_2024 = collection("alf/<probe>/pykilosort/#rev#")
    bucket = [
        Part("spike sorting, 2022", "Kilosort 2.5 (pykilosort) spikes, clusters, channels, templates",
             "data/<session>/alf/<probe>/pykilosort/", "ALF: .npy, .pqt, .csv", *files_2022,
             "in store: tag sort-2022", True,
             [("20 s of spikes by depth, 2022", cached("raster-2022.png", lambda: raster_picture(repo, "sort-2022")),
               "spikes.times, spikes.depths")]),
        Part("spike sorting, 2024 re-sort", "the whole dataset sorted again; more units per probe",
             "data/<session>/alf/<probe>/pykilosort/#2024-05-06#/", "ALF: .npy, .pqt, .csv", *files_2024,
             "in store: main, tag sort-2024", True,
             [("20 s of spikes by depth, 2024", cached("raster-2024.png", lambda: raster_picture(repo, "sort-2024")),
               "spikes.times, spikes.depths"),
              ("largest cluster, 8 channels", cached("waveform.png", lambda: waveform_picture(repo)),
               "clusters.waveforms")]),
        Part("raw ephys, compressed", "the same AP and LF samples as the raw NWB, with SpikeGLX metadata",
             "data/<session>/raw_ephys_data/<probe>/", ".cbin (mtscomp), .meta, .ch, QC .npy",
             *collection("raw_ephys_data/<probe>"), "left out: duplicates raw NWB; .meta in store", False),
        Part("session ALF", "trials, wheel, licks, camera times, pose, passive stimuli as ALF files",
             "data/<session>/alf/ and alf/#<date>#/", "ALF: .npy, .pqt", *collection("alf"),
             "left out: in the processed NWB", False),
        Part("raw video", "the cameras' video as IBL keeps it", "data/<session>/raw_video_data/",
             "MP4, H.264", *collection("raw_video_data"), "left out: video", False),
        Part("session sync and passive", "NI-DAQ sync channels and the passive protocol's stimuli",
             "data/<session>/raw_ephys_data/, raw_passive_data/", ".cbin, .npy, .bin",
             *collection("raw_ephys_data"), "left out: sync is in the raw NWB", False),
    ]
    other = []
    for prefix, title, what, fmt, store, picture in [
        ("aggregates/", "release tables", "every release's cluster and trial tables, across sessions",
         ".pqt, .npy", "left out: tables, phase 2 (Iceberg)",
         ("units / good units per release", lambda: release_units_picture(), "clusters.pqt, label")),
        ("atlas/", "atlas", "Allen atlas resources: template, depths, flatmaps, MERFISH, gene expression",
         ".npy, .nrrd, .pqt, .glb", "left out: reference data",
         ("200 µm template, probe00's channels", lambda: atlas_picture(repo), "atlas/agea/image.npy")),
        ("caches/", "ONE caches", "IBL's listings of each release: sessions and files with sizes and MD5",
         "zip of .pqt", "used to list files", None),
        ("spikesorting/", "spike-sorting benchmarks", "benchmark recordings and course data for sorters",
         ".npy, .bin, ALF", "left out: not the release", None),
        ("resources/", "resources", "compressed LF bands, agent data, other recordings",
         ".h5, .tar, raw data", "left out: not the release", None),
        ("paper_reproducible_ephys/", "reproducibility paper", "the repeated-site paper's models and data",
         ".npy, .pqt, figures", "left out: not the release", None),
        ("sample_data/", "sample data", "tutorial and course datasets", ".zip, ALF", "left out: not the release", None),
    ]:
        files, bytes_ = bucket_listing(prefix)
        pictures = [(picture[0], cached(f"{prefix.strip('/')}.png", picture[1]), picture[2])] if picture else []
        other.append(Part(title, what, prefix, fmt, f"{files:,}", size(bytes_), store, False,
                          pictures))
    other.append(Part("histology", "the brains' histology images", "not in the public bucket", "",
                      "0", "", "left out: not published; channel regions come from it", False))
    other.append(Part("all public data/", f"every public IBL session, {public['projects']} projects",
                      "data/<lab>/Subjects/<subject>/<date>/<number>/",
                      "largest: " + ", ".join(k for k, _ in public["folders"][:4]), f"{public['files']:,}",
                      size(public["bytes"]), "left out: the release is 480 of them", False))
    totals = (f"DANDI {DANDISET} v{DANDI_VERSION}: {len(assets):,} files, "
              f"{size(sum(a['size'] for a in assets))}")
    return [
        (f"DANDI {DANDISET}", totals, dandi),
        ("IBL bucket, the release in data/",
         f"s3://{IBL_BUCKET} (us-east-1), ONE cache Brainwidemap: "
         f"{len(one_cache('Brainwidemap')['sessions'])} sessions", bucket),
        ("IBL bucket, everything else",
         f"bucket-wide ONE cache of {public['date']}: {public['sessions']:,} sessions, "
         f"{size(public['bytes'])}", other),
    ]


def draw_part(canvas: Canvas, y: float, part: Part) -> float:
    status, tag = (("added", "in store") if part.held else
                   ("changed", "used") if part.store.startswith("used") else ("removed", "left out"))
    rows = [Row("what", part.what), Row("where", part.where), Row("format", part.format),
            Row("files", part.files), Row("size", part.size),
            Row("license", LICENSE if part.files != "0" else ""),
            Row("store", part.store.split(": ", 1)[-1], status=status, tag=tag)]
    bottom = d.panel(canvas, PANEL_X, y, PANEL_WIDTH, part.title, rows,
                     accent=ACCENTS["store"] if part.held else ACCENTS["source"])
    for i, (caption, image, source) in enumerate(part.pictures):
        x = PICTURE_X + i * (PICTURE_WIDTH + PICTURE_GAP)
        bottom = max(bottom, d.picture(canvas, x, y, image, PICTURE_WIDTH, caption=caption, source=source))
    return bottom


def archive_figure(repo, out: Path) -> int:
    """Write ``archive.svg`` into ``out`` and return its size in bytes."""
    canvas = Canvas(WIDTH)
    y = d.heading(canvas, "IBL Brain-Wide Map: the whole archive",
                  f"what the archive publishes, by host; pictures from {SESSION_PATH} ({LICENSE})")
    for host, subtitle, products in parts(repo):
        y += 34
        canvas.text(PANEL_X, y, host, size=17, weight="bold")
        y = canvas.text(PANEL_X, y + 18, subtitle, size=12, color=d.MUTED, mono=True) + 12
        for part in products:
            part.store = part.store.replace("{session}", "sub-<subject>/ses-<eid>")
            y = draw_part(canvas, y, part) + 22
    y += 34
    canvas.text(PANEL_X, y, "Release history", size=17, weight="bold")
    y = canvas.text(PANEL_X, y + 18, RELEASE_NOTES, size=12, color=d.MUTED, mono=True) + 12
    d.panel(canvas, PANEL_X, y, PANEL_WIDTH + 400, "releases", [Row(when, what) for when, what in RELEASES],
            accent=ACCENTS["highlight"])
    return canvas.write(out / "archive.svg")
