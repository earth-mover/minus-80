# IBL Brain-Wide Map: sources and layout

The International Brain Laboratory (IBL) Brain-Wide Map is a set of
Neuropixels recordings from mice doing a visual decision task: 459 sessions,
699 probe insertions, 139 mice, 12 labs
([2025 release notes](https://docs.internationalbrainlab.org/notebooks_external/2025_data_release_brainwidemap.html)).
This file records where each part of the data lives, which sessions phase 1
takes, how each source file maps onto the store, and what the source taught.
PLAN.md argues the decisions.

The ingest is named `ibl-brain-wide-map` after the AWS Open Data registry
entry and the IBL release tag (`Brainwidemap`).

## Where the data lives

### DANDI 000409

Version 0.260309.1324, published 2026-03-09, holds 2,048 assets and 49.7 TB
([DANDI](https://dandiarchive.org/dandiset/000409/0.260309.1324)). The license
in the dandiset metadata is `spdx:CC-BY-4.0`.

| Asset kind | Files | Size |
| --- | ---: | ---: |
| `desc-raw_ecephys.nwb` | 459 | 43.45 TB |
| `VideoLeftCamera.mp4` | 437 | 3.28 TB |
| `VideoRightCamera.mp4` | 433 | 2.19 TB |
| `desc-processed_behavior+ecephys.nwb` | 459 | 0.49 TB |
| `VideoBodyCamera.mp4` | 260 | 0.30 TB |

A raw NWB file holds one session's recordings from both probes. Raw files run
from 18 to 197 GB, with a median of 92 GB. For session ZM_2241 2020-01-28,
probe00:

| NWB path | Shape | Type | Chunks | Filter |
| --- | --- | --- | --- | --- |
| `acquisition/ElectricalSeriesProbe00AP/data` | 108,136,563 × 384 | int16 | 13,020 × 384 (10.0 MB) | gzip 4 |
| `acquisition/ElectricalSeriesProbe00AP/timestamps` | 108,136,563 | float64 | 1,250,000 | gzip 4 |
| `acquisition/ElectricalSeriesProbe00LF/data` | 9,011,381 × 384 | int16 | 13,020 × 384 | gzip 4 |
| `acquisition/ElectricalSeriesProbe00LF/timestamps` | 9,011,381 | float64 | 1,250,000 | gzip 4 |
| `acquisition/TimeSeriesImec0Sync/data` | 108,136,563 × 1 | int16 | 5,000,000 × 1 | gzip 4 |

The sample count depends on how long the recording ran; this one is 60
minutes. The AP band's 8,306 chunks hold 40.5 GB compressed, against 83.1 GB
of samples, a ratio of 2.05. IBL's own copy of the same band, the
mtscomp-compressed `.cbin`, is 29.4 GB.

The processed NWB file holds the trials table, wheel, pupil, lick and motion
energy series, the passive protocol, and a `units` table from the 2024 sort
(1,877 units for ZM_2241, the count IBL's 2024 cluster table gives for that
session).

The files were written with NeuroConv; the AP and LF voltages carry NWB's
`conversion` (2.34375e-06 V per count for AP, 4.6875e-06 for LF) and the unit
`volts`.

### The IBL public bucket

`s3://ibl-brain-wide-map-public` in us-east-1, listed on the
[AWS Open Data registry](https://github.com/awslabs/open-data-registry/blob/main/datasets/ibl-brain-wide-map.yaml)
under CC-BY 4.0. A session lives at
`data/<lab>/Subjects/<subject>/<date>/<number>/`, and holds every product as
ALF files: one `.npy` per attribute, named `<object>.<attribute>`, stored with
its dataset id before the extension. The raw recordings are there too, as
mtscomp `.cbin` files, which this ingest does not use.

Spike sorting for probe00 sits in `alf/probe00/pykilosort/`. The 2024 re-sort
sits beside it in the revision folder `alf/probe00/pykilosort/#2024-05-06#/`.
ALF's convention is that a revision is "a dated folder surrounded by pound
signs" ([ALF spec](https://int-brain-lab.github.io/ONE/alf_intro.html)).

IBL publishes the listing of every release as a ONE cache:
`caches/alyx/<tag>/cache.zip` holds `sessions.pqt` and `datasets.pqt`, which
give each file's session, path, size and MD5 hash. The ingest reads files from
these listings and never lists the bucket.

| Cache | Sessions | Datasets | Sort folders for `spikes.times` |
| --- | ---: | ---: | --- |
| `2022_Q4_IBL_et_al_BWM` | 354 | 38,814 | 546 `pykilosort/`, 1 `#2022-08-04#` |
| `Brainwidemap` (2025-02-20) | 480 | 76,563 | 699 `#2024-05-06#`, 698 `pykilosort/`, 1 `#2022-08-04#` |

The 2022 sort's files were left in place when the re-sort was published: all
546 `pykilosort/spikes.times.npy` files have the same hash in both caches.

The bucket also holds each release's cluster table in `aggregates/`:

| Release | Sessions | Probes | Units | Good units |
| --- | ---: | ---: | ---: | ---: |
| `2022_Q4_IBL_et_al_BWM` | 354 | 547 | 295,501 | 32,766 |
| `2024_Q2_IBL_et_al_BWM` | 459 | 699 | 621,733 | 75,708 |

### Release history

From the [2025 release notes](https://docs.internationalbrainlab.org/notebooks_external/2025_data_release_brainwidemap.html):

- 2022 Q4: first release, 354 sessions.
- 2024-02-15: 105 sessions added; 285 videos had timestamps corrected and 139
  were removed.
- 2024 re-sort: the whole dataset was spike-sorted again. The notes date the
  re-run 2024-05-15; its files sit in revision folders dated 2024-05-06.
- 2025 Q1: wheel polarity corrected in 62 sessions; audio sync patched.
- 2025 Q3 and Q4: passive intervals, Lightning Pose and saturation intervals
  added; passive Gabor timing fixed.

## The sessions of phase 1

Phase 1 takes 10 sessions. Each one:

- is in both the 2022 and the 2024 release, with two probes in both;
- has both sorts' files for both probes in IBL's listing;
- has its raw NWB file on DANDI.

193 sessions meet the first and third conditions, and 192 the second as well.
From those, one session was taken per lab, choosing at each step the session
that added the most new top-level (Cosmos) brain regions among its good units,
then the most good units. ZM_2241 was fixed first, because the trial used it.
The ten cover ten labs and all ten Cosmos regions.

| Session | Lab | Raw NWB | Good units 2022 | Good units 2024 | Main regions |
| --- | --- | ---: | ---: | ---: | --- |
| ZM_2241 2020-01-28 001 | mainenlab | 91.6 GB | 195 | 254 | CNU, TH, Isocortex |
| NR_0020 2022-05-09 001 | steinmetzlab | 122.6 GB | 136 | 282 | MB, HPF, Isocortex, CTXsp |
| CSHL060 2020-03-11 004 | churchlandlab | 163.4 GB | 154 | 252 | Isocortex, OLF, HB, CB |
| SWC_043 2020-09-21 001 | hoferlab | 111.4 GB | 217 | 448 | CNU, OLF, HY, Isocortex |
| DY_016 2020-09-11 001 | danlab | 108.8 GB | 144 | 469 | HB, TH, CB |
| NYU-37 2021-01-30 001 | angelakilab | 111.7 GB | 236 | 439 | CB, HB |
| SWC_054 2020-10-05 001 | mrsicflogellab | 106.5 GB | 178 | 424 | TH, HB, CB, Isocortex |
| UCLA037 2022-02-02 001 | churchlandlab_ucla | 129.7 GB | 196 | 376 | TH, MB, HPF, Isocortex |
| CSH_ZAD_025 2020-07-30 001 | zadorlab | 150.8 GB | 227 | 376 | MB, HPF, Isocortex |
| ibl_witten_26 2021-01-30 001 | wittenlab | 153.9 GB | 102 | 367 | HB, CB |

Their raw NWB files total 1.25 TB. The eids are in `ingest.SESSIONS`.

## The archive map

`diagrams/archive.svg` is a tour of the whole archive, top down: DANDI
000409's three kinds of file, then the release's collections in the IBL
bucket's `data/`, then the bucket's other prefixes, then the release
history. Each part has a panel saying what it is, where it lives, its format,
file count, size and license, and whether the store holds it, and most have
small pictures of the trial session's data:

- the raw NWB: a third of a second of AP voltage on every other channel
  (each channel's median removed), and five seconds of LF on six channels;
- the processed NWB: each trial's contrast by side, colored by reward, and two
  minutes of wheel position and pupil diameter;
- the videos: one left-camera frame, a minute in;
- the two sorts: twenty seconds of spikes by depth at each tag, and the
  largest 2024 cluster's mean waveform on eight channels;
- the release tables: units and good units in each release's cluster table;
- the atlas: a sagittal slice of IBL's 200 µm Allen template at probe00,
  with the probe's channels at their Allen CCF positions.

Counts and sizes come from DANDI's asset list, IBL's `Brainwidemap` ONE cache
(the release), its bucket-wide ONE cache (everything public in `data/`,
3,124,616 files and 488 TB on 2026-04-06) and listings of the other
prefixes. No histology images are public: the bucket-wide cache lists no
histology dataset.

## From source to store

The figures are in [diagrams/](diagrams/): `source.svg`, `mapping.svg` and
`store.svg`.

```
/                                        what the store is, license, citations, sources, conventions
sub-<subject>/ses-<eid>/                 what the session holds; eid, lab, date, IBL folder; both NWB files' DANDI paths, URLs, hashes
  raw/                                   DANDI's raw NWB file, whole: an NWBFile
  processed/                             DANDI's processed NWB file, whole: an NWBFile
  alf/probe00/pykilosort/                one sort; its year and IBL revision in attributes
    spikes/      times, clusters, amps, depths, samples, templates   (spike)
    clusters/    amps, channels, depths, peakToTrough, waveforms,
                 waveformsChannels, uuids                            (cluster, ...)
    channels/    brainLocationIds_ccf_2017, localCoordinates, mlapdv,
                 rawInd, labels                                      (channel, ...)
    templates/   amps, waveforms, waveformsChannels                  (template, ...)
  alf/probe01/pykilosort/
```

- **NWB files.** Each file is written whole in the unified NWB Zarr v3
  convention by shadowzarr, through `minus80.parsers.nwb.unified`. Arrays of
  1,000 elements or more are referenced chunk for chunk, the HDF5 gzip
  filter becoming Zarr's zlib codec, and carry DANDI's ETag; strings, scalars
  and smaller arrays are written inline. The group is placed under the
  session and otherwise left as shadowzarr maps it.
- **Reading.** zarr and xarray read every group with nothing else installed:
  the arrays' codecs are `bytes`, `zstd`, `vlen-utf8` and numcodecs'
  `zlib`, which zarr-python installs, and none uses shadowzarr's `keep_bytes`
  codec (checked on every array of the trial session). hdmf-zarr's
  `NWBZarrIO` opens only a store's root, so pynwb cannot open a session's NWB
  group in this store until hdmf-zarr accepts a group (docs/upstream.md).
- **Dimension names.** Every array gets `dimension_names`: in a TimeSeries,
  `time` for `data`, `timestamps` and anything as long, and `channel` for an
  ElectricalSeries' second axis and its `electrodes`; in a table, the table's
  own name for every column as long as `id`; elsewhere `<array>_dim<n>`.
- **Voltages.** Each ElectricalSeries' `data` keeps NWB's `conversion`,
  `offset` and `unit: volts`, and gains CF `scale_factor` and `add_offset`
  equal to them, `units: V`, and `coordinates: timestamps electrodes`. The
  timestamps stay a coordinate, not an index, because xarray loads an index
  whole on open: 108 million values, 525 MB compressed, for one AP band.
- **SpikeGLX meta.** Each ElectricalSeries group carries its band's `.meta`
  file from the IBL bucket as `spikeglx_meta`: the nominal sampling rate
  (`imSampRate`), gains, probe serial number and acquisition settings.
- **Sync channel.** `TimeSeriesImec0Sync/data` states `unit: uV` and
  `conversion: 1171.875`, as NeuroConv writes for any SpikeGLX channel, but
  holds a 16-bit status word whose bit 6 is the 1 Hz sync square wave; the
  store keeps NWB's attributes and adds no CF scale, so xarray returns the
  integers.
- **Spike sorting.** Each ALF `.npy` is referenced past its header, and its
  references carry the file's S3 ETag (also in the array's `source_etag`),
  so a read fails if IBL replaces the file. The ETag equals the MD5 in IBL's
  listing only for files uploaded in one part: 31 of 48 files checked; the
  others, over 8 MB, have multipart ETags of the form `<hex>-<parts>`.
  Per-spike columns are chunked every 262,144 rows (2 MB of float64); the rows
  past the last full chunk are written natively, because Zarr cannot decode a
  short chunk. Per-cluster, per-channel and per-template files are one chunk
  each. `clusters.uuids.csv` is text and is written natively. The path
  `alf/<probe>/pykilosort` is IBL's own collection path.
- **Revisions.** A sort replaces the probe's whole `pykilosort` group, because
  its arrays change length. Each sort is its own commit with its own tag.

Left out of phase 1, with the reason:

- the camera videos (5.8 TB of mp4), which need a video reader, not an array;
  the NWB files keep their `external_file` pointers;
- `clusters.metrics.pqt` and IBL's other Parquet tables, which have no Zarr
  convention and belong in Iceberg tables beside the store;
- `drift`, `waveforms.*` and `_phy_spikes_subset.*` (2 to 3 GB of waveform
  snippets per probe) and `_kilosort_whitening.matrix`;
- the `.cbin` raw files, which need an mtscomp codec and duplicate the NWB.

## What the source taught

- **The electrodes table does not list probe00 first.** In ZM_2241's file, rows
  0 to 383 are Probe01 and rows 384 to 767 are Probe00. An ingest that took
  the first 384 rows for probe00 would attach the other probe's geometry and
  brain regions without any error. The table's `id` column equals the row position (0 to 767), so the
  series' `electrodes` values are both.
- **The timestamps are a straight line.** Over the AP band's first and last
  200,000 samples, a line fits within 77 ns, at 30,000.31 Hz; the LF band fits
  within 1.2 µs at 2,500.03 Hz. NWB could have stored a `starting_time` and
  `rate`; the files store every timestamp, 525 MB compressed per AP band.
- **The 2024 re-sort finds many more spikes.** ZM_2241's probe00 has 8,701,439
  spikes in 564 clusters in the 2022 sort and 21,415,765 spikes in 776 clusters
  in the 2024 sort.
- **The re-sort's folder date and the release notes differ.** The folders say
  2024-05-06; the notes say the re-run was on 2024-05-15.
- **ALF states units only for times.** The ALF spec says timestamp datasets
  use a common timescale, "usually seconds". No IBL page found gives units
  for `spikes.amps`, `spikes.depths` or `clusters.depths`, so those arrays
  carry no `units` attribute. The processed NWB names its own columns
  `spike_amplitudes_uV` and `distance_from_probe_tip_um`.
- **`#` in a folder name broke key resolution.** obspec-utils' registry
  compared the store's decoded prefix with the URL's percent-encoded path, so
  every file under `#2024-05-06#` resolved to a key that repeated the prefix
  and returned 404. `minus80.core.registry.key_within` fixes it for every
  remote store. Icechunk reads a virtual chunk whose URL holds `%23`.
- **DANDI's `+` filenames do not reach the store.** The store references DANDI's
  content-addressed blobs (`s3://dandiarchive/blobs/<a>/<b>/<uuid>`), which have
  no `+`, and keeps the asset path in the session's attributes.
- **NWBZarrIO reads the convention, but only at a store's root.** hdmf-zarr
  0.14's `NWBZarrIO` read ZM_2241's raw NWB file written at the root of an
  Icechunk store, with the added `dimension_names`, CF attributes and
  `spikeglx_meta` in place, and read both of the session's files when a test
  wrapper presented their groups as a store's root. Given the store itself,
  it raises "Found bad link to object" with a path inside the NWB group;
  given a `zarr.Group` or a `StorePath`, it raises "incorrect type for
  'path'".
- **An HTTPS URL to `dandiarchive.s3.amazonaws.com` hangs.** obstore signs
  requests to an S3 host and looks for EC2 credentials; with the patient retry
  settings an index walk sat for minutes before failing. The `s3://` URL with
  anonymous access reads at once.

## Judgement calls

- Session groups are named `sub-<subject>/ses-<eid>`, as DANDI names the
  session's files (`sub-ZM-2241/sub-ZM-2241_ses-<eid>_desc-raw_ecephys.nwb`).
  The `sub-`/`ses-` form is BIDS's, which DANDI and the NWB tools follow, so
  a neuroscientist recognizes it, and a reader finds the same names in the
  dandiset. IBL's own folder, `<lab>/Subjects/<subject>/<date>/<number>`, is
  in each session's attributes; its `alf/<probe>/pykilosort` collection path
  is kept inside the session (PLAN.md).
- One repo holds the dataset, with each NWB file at a group, although
  pynwb opens an NWB file in Zarr only at a store's root. A repo per file
  would let pynwb open it now, and would split one dataset into about 900
  repos (PLAN.md).
- Each NWB file is its own group, `raw` or `processed`, rather than the
  session group itself, so the session can hold both files and the ALF sorts
  without putting foreign groups inside an NWBFile (PLAN.md).
- The CF scale on voltages gives volts, the unit NWB states on the same array,
  so pynwb and xarray agree (PLAN.md).
- Spike columns are chunked every 2^18 rows, which leaves at most 2 MB per
  column to write natively (PLAN.md).
- Dimension names line up within a group rather than following NWB's schema,
  whose names differ between siblings: an ElectricalSeries' `data` is
  `num_times, num_channels` and its `electrodes` is `num_rows`.
- `units` on voltages and times is a CF string. minus80's own builders read
  `units` as OME-Zarr's per-axis mapping, so the ingest sets it after
  building each ALF array.
