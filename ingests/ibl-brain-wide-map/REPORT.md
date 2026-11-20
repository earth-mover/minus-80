# IBL Brain-Wide Map: report

## What was built

`earthmover-biology/ibl-brain-wide-map` on Arraylake holds ten sessions of the
International Brain Laboratory's Brain-Wide Map: Neuropixels recordings from
mice making visual decisions, one session from each of ten labs, covering
all ten of the atlas's top-level brain regions (LAYOUTS.md lists them).

Each session is a group `sub-<subject>/ses-<session id>`, named as on DANDI:

- `raw`: the session's raw NWB file from DANDI 000409, with both probes' action
  potential band (30 kHz) and local field potential band (2.5 kHz) over 384
  channels, every channel's position and brain region, and the sync channel;
- `processed`: the session's processed NWB file, with the trials, wheel,
  pupil, licks, pose, passive stimuli and the units of IBL's 2024 sort;
- `alf/<probe>/pykilosort`: IBL's spike sorting of each probe, from the IBL
  public bucket, as ALF objects (spikes, clusters, channels, templates).

The store has two versions of the spike sorting:

| Version | Snapshot | Spike sorting |
| --- | --- | --- |
| tag `sort-2022` | R4C3JACGSHGCB83062AG | IBL's 2022 release (`pykilosort/`) |
| tag `sort-2024`, and `main` | K2FPGX52P75CHCJDJXAG | IBL's 2024 re-sort (`pykilosort/#2024-05-06#/`) |

The sessions' NWB files total 1.25 TB raw and 16 GB processed, and the store
copies none of it: every array of 1,000 elements or more reads its chunks
from the original files. DANDI's are pinned to the file's ETag, and IBL's to
each `.npy` file's S3 ETag. The store holds about 4,140 arrays, a count summed
from the pre-run survey of its files rather than counted on the store.

To open it, with an Arraylake login, `zarr` and `xarray`:

```python
from arraylake import Client
import xarray as xr

repo = Client().get_repo("earthmover-biology/ibl-brain-wide-map")
store = repo.readonly_session("main").store          # or tag="sort-2022"
session = "sub-ZM-2241/ses-465c44bd-2e67-4112-977b-36e1ac7e3f8c"
ap = xr.open_zarr(store, group=f"{session}/raw/acquisition/ElectricalSeriesProbe00AP",
                  consolidated=False)
```

`ibl-brain-wide-map.ipynb` opens it cold. The NWB groups follow the unified
NWB Zarr v3 convention of hdmf-zarr 0.14, but hdmf-zarr's `NWBZarrIO` opens
only a store's root. pynwb cannot open a session's NWB group in this store
until hdmf-zarr accepts a group; zarr and xarray read every group now.

## How it was run

| Run | Code | Result |
| --- | --- | --- |
| [runs/01_ten_sessions.sh](runs/01_ten_sessions.sh) | 310cad5 (tag `ibl-run1-ten-sessions`) | committed session 1 (7CSTFJXAM55CJG0D0WW0), then failed on session 2 and did not end; stopped |
| [runs/02_resume_ten_sessions.sh](runs/02_resume_ten_sessions.sh) | cdc0223 (tag `ibl-run2-resume`) | skipped session 1, wrote the rest, both tags, verify passed; 11 min 12 s on a c6in.xlarge in us-east-1 |

Run 2's 2022 pass took 497 s for nine sessions, about 55 s each, of which each
commit took 4 s. The 2024 pass took 136 s for ten sessions. Peak memory was
1.8 GB.

## Verification

`verify.py` ran at the end of run 2 and passed every check in 22 s. On every
session and probe, without reading the sources, it checked:

- both NWB groups and the four ALF objects are present;
- every voltage array has axes `(time, channel)`, a CF scale equal to NWB's
  conversion, timestamps and electrodes as long as its axes, and its SpikeGLX
  meta;
- every probe holds the 2022 sort at `sort-2022` and the 2024 sort at
  `sort-2024` and `main`;
- every array's codecs are ones zarr-python reads alone: bytes, zstd,
  vlen-utf8 and numcodecs.zlib;
- every spike column is as long as its `.npy` file, by the file's size and
  its header.

On one session and probe, it read the sources and checked:

- an AP window across a chunk boundary and the LF band's last 500 samples
  equal h5py's decode of the NWB file;
- a spike-time chunk and the natively written last rows equal the `.npy`
  bytes, at each tag;
- xarray returns volts with no sample masked as missing.

The notebook then ran end to end against the published store, in an
environment with neither minus80 nor shadowzarr installed.

Before run 2, every file the run would read was surveyed. shadowzarr mapped
all 20 NWB files, with dimension names and codecs checked and nothing
written, and the headers of all 780 ALF files of both sorts were read.

## Changes from the plan

- Each session is its own commit, rather than one commit per pass, so a run
  that stops can be launched again and resumes: a pass whose tag exists is
  skipped, and a session that `main` already holds with that pass's sort or
  a later one is skipped.
- Every ALF reference is pinned to its file's S3 ETag, read with a HEAD and
  checked against the MD5 in IBL's listing for single-part uploads.
- The 2022 sorts' `clusters.brainLocationAcronyms_ccf_2017` is left out and
  named in each sort group's `left_out` attribute: it is a pickled object
  array, which no byte range can hold.
- The run took two runs. Run 1 found that seven of the ten raw NWB files have
  contiguous datasets, which the trial session did not have, and that its
  script hung on failure instead of ending. Both were fixed for run 2.

## Judgement calls made

- Each NWB file is written whole at its own group, in the unified NWB Zarr v3
  convention, with xarray `dimension_names` and a CF scale added.
- Session groups use DANDI's `sub-`/`ses-` names; IBL's own folder, lab, date
  and number are in each session's attributes.
- The CF scale on voltages gives volts, the unit NWB states, so pynwb and
  xarray agree.
- The two sorts are successive commits with tags, not branches.
- Spike columns are chunked every 2^18 rows, with the short last chunk native.
- Each session's description names its probes' brain regions at the atlas's
  top level, most channels first.
- Videos, Parquet tables and waveform snippets are left out (LAYOUTS.md).

## Problems found in the source data

- The raw NWB electrodes table lists probe01's channels before probe00's; the
  ElectricalSeries' own `electrodes` rows are used.
- The timestamps are a straight line to within 77 ns over an hour, yet stored
  one per sample: 525 MB compressed per AP band.
- `TimeSeriesImec0Sync` states `unit: uV` and `conversion: 1171.875` but holds
  a 16-bit status word.
- The re-sort's folders are dated 2024-05-06; the release notes date the re-run
  2024-05-15.
- The 2022 sort's acronym column is a pickled object array.
- IBL's documentation states units only for times; no units are given for
  spike amplitudes or depths, so those arrays carry none.
- Seven of the ten raw NWB files store their `acquisition/Events*` tables
  contiguously, unlike the others.

## Upstream issues hit

All are listed in [docs/upstream.md](../../docs/upstream.md):

- shadowzarr has no release; the workspace pins a commit.
- shadowzarr's `rfs_to_icechunk` writes only at a session's root, and
  `generate_rfs` writes no dimension names.
- hdmf-zarr's `NWBZarrIO` opens only a store's root. No existing hdmf-zarr
  issue was found.
- VirtualiZarr's `to_icechunk` cannot pin each source object to its ETag.
- obspec-utils' registry fails on folder names that need percent-encoding,
  such as IBL's `#2024-05-06#`.
