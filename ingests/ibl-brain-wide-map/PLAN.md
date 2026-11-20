# IBL Brain-Wide Map: plan

## Summary

Phase 1 builds one Icechunk store, `earthmover-biology/ibl-brain-wide-map`,
that references 10 IBL Brain-Wide Map sessions (20 probes) without copying
them:

- each session's two NWB files from DANDI 000409 (1.25 TB of raw recordings
  and 16 GB of processed behavior and units), each written whole in the
  unified NWB Zarr v3 convention of hdmf-zarr 0.14, with dimension names on
  every array, so zarr and xarray open any of its series or tables;
- the spike sorting from the IBL bucket, as two commits: the 2022 sort, tagged
  `sort-2022`, then the 2024 re-sort, tagged `sort-2024`. A reader checks out
  either tag and gets that sort's spikes and clusters at the same paths.

The re-sort is the reason to use Icechunk here: IBL published it beside the
original, in a dated folder, and the store turns that into a version history a
reader can switch between.

A one-session trial on a laptop built both NWB groups and both sorts for two
probes in 31 s. The store check passed in 10 s, every sample and spike time it
compared was equal to the source, and the notebook ran in an environment with
only zarr, xarray and icechunk installed. The run script is
`runs/01_ten_sessions.sh`; the full run is estimated at under $1 on Coiled.

**Asked of Ian:** approve the plan and choose on each decision below; each
has a recommendation. Writing to Arraylake needs Ian to run the calls in
"Arraylake setup" below: two bucket configs, two access policies and the repo.

### Decisions

| # | Decision | Recommendation | Alternatives |
| --- | --- | --- | --- |
| 1 | Layout of the NWB data | each NWB file whole at `<session>/raw` and `<session>/processed`, in the unified NWB Zarr v3 convention, with xarray `dimension_names` and CF scales added; one repo for the dataset | a repo per NWB file; the session group as the NWB file; hand-built xarray groups |
| 2 | How the two sorts are kept | commit the 2022 sort, tag it, replace it with the 2024 sort, tag that; `main` holds 2024 | a branch per sort; both sorts side by side in one snapshot |
| 3 | Chunking of the per-spike columns | 2^18 rows per chunk, the short last chunk native | one chunk per file; 2^16 rows |
| 4 | Session group names | `sub-<subject>/ses-<eid>`, as DANDI names the files | IBL's `<subject>/<date>/<number>`; the eid alone |
| 5 | Unit of the CF scale on voltages | volts, the unit NWB states, so pynwb and xarray agree | microvolts |
| 6 | Sessions | the 10 in LAYOUTS.md: ten labs, all ten top-level regions | any of the 192 that qualify |
| 7 | `clusters.metrics.pqt` and IBL's other Parquet tables | phase 2, as Iceberg tables beside the store | Zarr arrays now |

Decision 1 needs the most attention: pynwb cannot open the NWB groups in this
store until hdmf-zarr accepts a group, and the convention is new (hdmf-zarr
0.14, released 2026-09-22; shadowzarr, unreleased).

## The archive

The International Brain Laboratory recorded mice doing a visual decision task
with Neuropixels probes, sampling nearly every brain region across 12 labs:
459 sessions, 699 probe insertions, 139 mice, 621,733 sorted units of which
75,708 pass quality control
([release notes](https://docs.internationalbrainlab.org/notebooks_external/2025_data_release_brainwidemap.html);
[paper](https://doi.org/10.1038/s41586-025-09235-0)). Two hosts publish it:

- **DANDI 000409**, 49.7 TB in 2,048 assets: per session a raw NWB file (459
  files, 43.45 TB, median 92 GB), a processed NWB file (0.49 TB) and camera
  videos (5.8 TB). The raw NWB holds both probes' AP band (30 kHz) and LF band
  (2.5 kHz) as int16, gzip level 4, in 10 MB chunks of 13,020 samples by 384
  channels. The processed NWB holds trials, wheel, pupil, licks, pose and the
  2024 sort's units.
- **The IBL bucket** `s3://ibl-brain-wide-map-public`: every product as ALF
  files, one uncompressed `.npy` per attribute, with each spike sort's files
  and each re-sort in a dated revision folder. IBL's ONE caches list every
  file of a release with its size and hash.

**License: CC BY 4.0, checked at both sources.** The dandiset's metadata says
`spdx:CC-BY-4.0`, and the AWS Open Data registry entry for the bucket says
`CC-BY 4.0`. The Brain-Wide Map is one study from one consortium, so there are
no component licenses to check.

People use it through IBL's ONE API, which downloads ALF files by session and
revision, or by streaming NWB files from DANDI. Reading one minute of one
probe's AP band means downloading the whole raw file (median 92 GB) or
walking its HDF5 index remotely on every open.

LAYOUTS.md has the full listing, the release history and the corrections to
the scope first proposed: a raw AP band's length depends on the recording
(108.1 million samples for the trial's 60-minute session, not a fixed 202.6
million), and DANDI's `+` filenames never reach the store.

## Proposed layout

The figures, drawn from the trial store and the source listings:
[source](diagrams/source.svg), [mapping](diagrams/mapping.svg),
[store](diagrams/store.svg).

```
/                                            title, what the store holds, license, citations, sources, conventions
sub-<subject>/ses-<eid>/                     what the session holds; eid, lab, date, IBL folder; NWB paths, URLs, hashes
  raw/                                       DANDI's raw NWB file: an NWBFile
    acquisition/ElectricalSeriesProbe00AP/   SpikeGLX meta in attributes
      data        (time, channel) int16      referenced; NWB conversion, and a CF scale_factor to volts
      timestamps  (time) float64             referenced, seconds
      electrodes  (channel) int64            rows of the electrodes table
    acquisition/ElectricalSeriesProbe00LF/, ElectricalSeriesProbe01AP/, TimeSeriesImec0Sync/, ...
    general/extracellular_ephys/electrodes/  (electrodes) probe geometry, CCF position, brain region
    intervals/epochs/, general/, specifications/
  processed/                                 DANDI's processed NWB file: an NWBFile
    intervals/trials/  (trials)              the task's trials
    units/                                   the 2024 sort, as NWB stores it
    processing/{wheel,pupil,lick_times,motion_energy,pose_estimation,passive_protocol}/
  alf/probe00/pykilosort/                    one sort: its year and IBL revision in attributes
    spikes/ clusters/ channels/ templates/   ALF attributes as arrays on the object's axis
  alf/probe01/pykilosort/
```

| Part | Referenced | Native | Phase-1 size |
| --- | --- | --- | --- |
| Raw NWB | every array of 1,000 elements or more | strings, scalars, arrays under 1,000 elements | 1.25 TB of source |
| Processed NWB | the same | the same | 16 GB of source |
| Spike sorting, per sort | every `.npy` | each spike column's last partial chunk; cluster UUIDs | about 10 MB per probe per sort |

One session holds 431 arrays: 111 in `raw`, 278 in `processed` and 42 in
`alf`. Phase 1 holds about 4,300.

### Decision 1: the NWB layout

Each NWB file is written whole at its own group, in the convention hdmf-zarr
0.14 reads from Zarr v3: links in a `_LINKS` group attribute, dataset types in
`_DTYPE`, scalars as shape-() arrays, references as `{"_REFERENCE": {"source",
"path"}}` ([storage.rst](https://github.com/hdmf-dev/hdmf-zarr/blob/0.14.0/docs/source/storage.rst);
[hdmf-zarr 0.14.0](https://github.com/hdmf-dev/hdmf-zarr/releases/tag/0.14.0)).
shadowzarr ([repository](https://github.com/bendichter/zindi)) builds the tree
from the file's headers, and its `rfs_to_icechunk` writes the chunks as
virtual references that carry DANDI's ETag, so a changed source file fails a
read instead of returning other bytes.

The group is placed under the session and otherwise left as shadowzarr maps
it. The ingest adds only what core Zarr metadata and CF already define:

- `dimension_names` on every array. In a TimeSeries, `data`, `timestamps` and
  `electrodes` share `time` and `channel`; in a table, every column shares the
  table's name. xarray opens any series or table group.
- On each voltage array, CF `scale_factor`, `add_offset` and `units` copied
  from NWB's `conversion`, `offset` and `unit`, so xarray returns volts.
  `timestamps` and `electrodes` are named as coordinates, not made indexes,
  because xarray loads an index whole on open: 108 million values, 525 MB
  compressed, for one AP band.

A reader needs zarr-python and icechunk, nothing of ours: every array's
codecs are `bytes`, `zstd`, `vlen-utf8` and numcodecs' `zlib`, and none uses
shadowzarr's `keep_bytes` codec. The tests write the same files at a store's
root and check that `NWBZarrIO` reads them, with the added attributes in
place, equal to the HDF5 files.

The tradeoffs:

- **pynwb cannot open a session yet.** `NWBZarrIO` opens only a store's
  root: given the store it fails on the first link ("Found bad link to
  object"), and given a group it refuses it ("incorrect type for 'path'").
  Until hdmf-zarr accepts a group, NWB users read the groups with zarr and
  xarray. No hdmf-zarr issue covers this; docs/upstream.md records the search.
- **The convention is new.** hdmf-zarr 0.14 is two months old, and
  shadowzarr has no release; the workspace pins a commit.
- **xarray sees a channel axis, not channel geometry.** The geometry is in the
  electrodes table group, and a series' `electrodes` coordinate gives the
  rows to take from it. The notebook shows the join.

Alternatives:

- **A repo per NWB file.** pynwb could open each repo's root now, but one
  dataset would become about 900 repos, with the sessions' sorts
  apart from their recordings.
- **The session group as the NWB file.** It does not help pynwb inside one
  repo, and the session holds two NWB files and the ALF sorts; an NWBFile
  holding foreign groups invites readers to treat them as NWB.
- **Hand-built xarray groups**, an earlier draft of this plan: `(time,
  channel)` arrays with native channel coordinates. They suit xarray, but
  drop NWB's structure, and the ingest would decide which of NWB's 389 arrays
  per session to keep.

### Decision 2: the two sorts

The 2022 and 2024 sorts are the same kind of data at the same paths, so they
fit one history: commit the 2022 sort with the NWB files and tag it
`sort-2022`, then replace each `alf/<probe>/pykilosort` group with the 2024
sort, commit and tag `sort-2024`. A reader on `main` gets the current sort; a
reader of an older analysis checks out `sort-2022`. A branch per sort would
say the two are parallel lines rather than one following the other. Both in
one snapshot would read without Icechunk's history, but would not show it.
The processed NWB's `units` table is the 2024 sort, the one DANDI publishes,
at both tags.

### Decision 3: chunking the spike columns

An uncompressed `.npy` has no internal unit; any byte range of it is a valid
chunk, and none needs decoding. One chunk per file means reading 171 MB to get
any spike time of a 21-million-spike probe. Chunks of 2^18 rows (2 MB of
float64, about 44 s of recording at this probe's spike rate) keep reads small.
Zarr cannot decode a short last chunk from a reference, so the rows past the
last full chunk are written natively, up to 2 MB per column: about 10 MB per
probe per sort, 0.4 GB for phase 1. Chunks of 2^16 rows would cut the native
part to about 0.1 GB and quadruple the chunk count.

### Decision 4: session names

Three naming schemes are in use for these sessions:

- DANDI files them as `sub-ZM-2241/sub-ZM-2241_ses-<eid>_desc-raw_ecephys.nwb`,
  the `sub-`/`ses-` form of BIDS, which DANDI and the NWB tools follow;
- IBL's ALF folders are `<lab>/Subjects/ZM_2241/2020-01-28/001`;
- IBL's code names a session by its eid alone.

The store uses DANDI's, `sub-ZM-2241/ses-<eid>`: a neuroscientist recognizes
the form, the NWB files inside are DANDI's, and a reader finds the same names
in the dandiset. The session's attributes give IBL's folder, lab, date and
number, and a `description` that says what the session group holds.

### Decision 5: volts

The first draft scaled voltages to microvolts. In the NWB layout, NWB states
`unit: volts` on the same array, and pynwb applies `conversion` to give volts.
A CF scale to microvolts on that array would make the two readers disagree by
a factor of a million, so the scale gives volts.

### Layouts considered and rejected

- **Copying the raw data.** The NWB chunks are 10 MB and gzip, readable by
  every Zarr client, so referencing them gives readers what a copy would.
- **The `.cbin` files.** They are smaller (29.4 GB against 40.5 GB for the
  trial's AP band) but need an mtscomp codec, and they duplicate the NWB.
- **The sorts from the processed NWB.** It holds only the 2024 sort, so the
  IBL bucket's ALF files are the only source of the 2022 sort.

## What has been tested

One session (ZM_2241 2020-01-28, both probes) on a laptop, writing a local
repo and reading the sources anonymously.

| Step | Measured |
| --- | --- |
| shadowzarr maps the 92 GB raw NWB (both probes; 1,941 references and 2 chunk indexes) | 6.3 to 7.4 s |
| shadowzarr maps the 0.96 GB processed NWB | 10.3 s |
| Ingest: both NWB files, the 2022 sort for both probes, commit | 24 s |
| Ingest: the 2024 sort for both probes, commit | 7 s |
| Repo on disk after both commits | 41 MB of native chunks, 2.1 MB of manifests |
| Commit of one session's 431 arrays to a local repo | 0.1 s |
| The notebook, with only zarr, xarray and icechunk installed | runs end to end |
| Store check (`verify.py`) from a clean environment | 10.5 s, all checks pass |

The store check compared, byte for byte, an AP window across a chunk
boundary and the last 500 LF samples
with h5py's decode of the NWB, and one spike-time chunk plus the native last
rows with the `.npy` bytes at each tag. The fixture tests (`test_ingest.py`
and `tests/parsers/nwb/test_unified.py`) check every array of a scaled-down
session the same way, and check that `NWBZarrIO` reads the files written at
a store's root equal to the HDF5 files. The notebook ran end to end against
the trial repo with zarr, xarray, icechunk, pandas and dask, and neither
minus80 nor shadowzarr, installed.

These reads were measured on the earlier draft of the trial, which
referenced the same chunks:

| Read | Time |
| --- | --- |
| 1 s of AP, all channels | 1.1 s |
| 60 s of AP, all channels (about 680 MB fetched) | 17.6 s |
| 10 s of one AP channel | 5.1 s |
| all 21.4 million spike times and cluster ids | 6.1 s |

The read of one channel fetches every channel, because the source's chunks
hold all 384 channels; the store keeps the source's chunking.

The local commit hides the per-object cost of an object store
(docs/ingest.md). One session's commit writes about 230 manifests and 140
native chunks; on Arraylake that is untimed.

Not tested: a session other than ZM_2241, writing to Arraylake, and the run
on Coiled.

## Steps, scope and risk

### Steps

1. Ian approves this plan and runs the calls in "Arraylake setup", then gives
   the ingest token write access to the repo in the web app.
2. Merge the shared minus80 changes (branch `encoded-keys-flat-rows`) to
   `main`, and rebase this branch on it.
3. Launch `runs/01_ten_sessions.sh`. Its launcher refuses uncommitted or
   unpushed code, tags the commit `ibl-run1-ten-sessions` and starts one
   Coiled batch job in us-east-1. The job writes each session in its own
   commit with the 2022 sort and tags `sort-2022`, writes each session's 2024
   sort and tags `sort-2024`, then runs `verify.py`. A session already on
   `main` with the pass's sort is skipped, so a stopped job can be launched
   again.
4. Record the result in the run script's header; run the notebook against
   Arraylake; write REPORT.md.

The job runs in us-east-1, where the IBL bucket is: it reads ALF files there
whole (the spike columns' last chunks), and only file headers and one verify
window from DANDI in us-east-2.

### Cost

| Item | Estimate |
| --- | --- |
| Coiled c6in.xlarge in us-east-1, about 20 minutes with install | under $0.20 |
| Requests to the sources: the headers of 20 NWB files and about 900 ALF files | under $0.01 |
| Arraylake storage: about 0.4 GB native (41 MB a session) plus 21 MB of manifests | cents a month |
| Total | under $1 |

The trial's times give the job's length: about 30 s a session for the NWB
files and the 2022 sort, 10 s for the 2024 sort, and the commits, which are
untimed on Arraylake. The run script's limits are 40, 20 and 5 minutes.

### Arraylake setup

For Ian to run from his login, after approving them. They mirror the
`camelyon-dataset` bucket config and the prefix-scoped `bossdb-open-data`
access policy. Check `c.list_bucket_configs("earthmover-biology")` first,
because a config for either bucket may already exist.

```python
from arraylake import Client

c = Client()
c.create_bucket_config(org="earthmover-biology", nickname="dandiarchive",
                       uri="s3://dandiarchive", region_name="us-east-2",
                       auth_config={"method": "anonymous"})
c.set_virtual_chunk_access_policy("earthmover-biology", bucket_nickname="dandiarchive",
                                  subprefix="blobs", public=True)
c.create_bucket_config(org="earthmover-biology", nickname="ibl-brain-wide-map-public",
                       uri="s3://ibl-brain-wide-map-public", region_name="us-east-1",
                       auth_config={"method": "anonymous"})
c.set_virtual_chunk_access_policy("earthmover-biology", bucket_nickname="ibl-brain-wide-map-public",
                                  subprefix="data", public=True)
c.create_repo("earthmover-biology/ibl-brain-wide-map", prefix="ibl-brain-wide-map",
              description="IBL Brain-Wide Map: DANDI 000409's NWB files and IBL's 2022 and 2024 "
                          "spike sorts, referenced in place",
              authorize_virtual_chunk_access={
                  "s3://dandiarchive/blobs/": "dandiarchive",
                  "s3://ibl-brain-wide-map-public/data/": "ibl-brain-wide-map-public"})
```

The access policies are scoped to the prefixes the store references:
DANDI's bucket also holds Zarr assets and other dandisets' files, and the
IBL bucket holds caches and aggregates outside `data/`.

### Risks

- **New software in the read path.** hdmf-zarr 0.14 is two months old, and
  shadowzarr is unreleased and pinned. A change to the convention would mean
  rewriting the NWB groups' metadata, not the references.
- **The references depend on two hosts keeping their files.** DANDI's blobs
  are content-addressed, belong to a published version and are read with
  their ETag. IBL left the 2022 sort in place when it published the 2024
  re-sort, but a future fix could replace files; a re-run of `verify.py`
  would find it.
- **IBL fixes more data later.** The 2025 fixes touched wheel and sync data,
  which live in the processed NWB on DANDI; a new dandiset version would be a
  new commit of that group. A future re-sort would be a third tag.
- **Snapshot growth.** About 4,300 arrays in phase 1; all 459 sessions would
  be about 200,000 arrays and 43 TB referenced.
- **Spike columns name no units except times.** IBL's documentation states
  none, so the store carries none rather than guess.
