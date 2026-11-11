# SanctSound: ingest plan

## Summary

The SanctSound store already exists. The Arraylake repo
`earthmover-biology/sanctsound` holds eight deployments of NOAA's hydrophone
recordings as byte-range references into the public bucket
`gs://noaa-passive-bioacoustic`, with NOAA's detections, hourly sound levels,
temperature records and calibration certificates written beside them. It was
built in September 2026 by code kept outside this repository. On 2026-10-27 it
was copied unchanged into `earthmover-biology` from `earthmover-demos/sanctsound`,
snapshot `5WAWRP95A0KKNDA64BSG`.

This plan brings that code into minus-80 as `ingests/sanctsound`, rebuilt on the
shared `minus80.parsers.flac` parser. It covers what has been done so far and
what a rebuild of the published store would change.

The port was approved, and the three deployments the published store held
only in part have since been rebuilt in full, committed straight to `main`.
[REPORT.md](REPORT.md) has the runs and their verification.

**Asked of the reviewer:** call 3 below is still open. Calls 1 and 2 were
taken as recommended.

| # | Decision | Recommended |
|---|---|---|
| 1 | Frames per chunk: 512, as published, or one, the FLAC frame | Keep 512 for the rebuild, and measure one-frame reads before changing it |
| 2 | The calibration offset, never run on the ported code | Measure it on Coiled as part of the rebuild |
| 3 | Where the saved chunk tables live | Beside the store, in the repo's bucket prefix |

## The archive

The NOAA-Navy Sanctuary Soundscape Monitoring Project (SanctSound) recorded
underwater sound in US national marine sanctuaries from 2018 to 2022. The
project has ended, so the archive is static. The bucket allows anonymous reads
and has no S3 copy. It holds 30 sites and 196 deployments: 158,544 FLAC
recordings (49.67 TB) and 19,626 product files (0.40 TB). The raw audio has DOI
[10.25921/saca-sp25](https://doi.org/10.25921/saca-sp25), and every derived
product has its own. NOAA's license text is carried verbatim in the store.

Every recording is mono 16-bit FLAC with a fixed blocksize. The sample rate and
blocksize vary between deployments, so a deployment is the unit of one array.
A recording's start time exists only in its filename, accurate to about 1.5 s.
[LAYOUTS.md](LAYOUTS.md) has the file layout, the filename forms, the timing
analysis and the defects in the source.

The published store held these deployments:

- `CI01_01`, `CI01_02`, `OC01_01`, `OC01_03` and `SB01_03` in full;
- `CI02_01`, `CI04_01` and `CI05_01` only for the recordings around four
  windows in November 2018: explosions, two ship passages and a blue whale bout.

All eight are now in full.

## Layout

One group per deployment, named like the archive. The full table is in
[LAYOUTS.md](LAYOUTS.md#layout-of-the-store). In short:

- `audio/waveform` packs every recording end to end on one sample axis. Its
  chunks reference the FLAC files, and the `flac` Zarr codec from
  [flac-numcodecs](https://github.com/ianhi/flac-numcodecs/tree/zarr3-flac-codec-2026-10-19)
  decodes them.
- `audio/segments` maps each recording's samples to its filename time.
- `detections`, `sound_levels`, `temperature` and `calibration` are written
  from NOAA's netCDF, `.rsk` and PDF files. They total about 205 MB, and the
  sound-level files cannot be referenced: `PSD_1h` is one contiguous 204 MB
  block.

The store is not OME-Zarr, because it holds no images. Its metadata follows
CF-1.8 and ACDD-1.3.

These layouts were considered and rejected when the store was first built:

- **Recordings placed at wall-clock positions.** The filename timestamps
  disagree with the sample counts by up to 1.5 s, and 345 of CI01_01's 354
  boundaries look like overlaps. Correlating the disputed samples gives
  r = 0.005, so the audio is distinct. Placing recordings by wall clock would
  discard real audio.
- **A regular chunk grid.** Each recording's last chunk would have to be
  zero-filled by the codec, and those zeros would sit mid-array, where Zarr
  returns them as data. The grid is rectilinear instead, ending each
  recording's last chunk at its last sample.
- **Indexing frames from the SEEKTABLE.** Files from 36 of the 196 deployments
  have none, so the scan reads every frame header instead.

## What has been tested

The port was checked on a laptop over a home connection, against a local
Icechunk repo with a virtual chunk container for the NOAA bucket. Every run
read the bucket anonymously, and the timings below are for that connection.

| Check | Result |
|---|---|
| `uv run pytest` | 234 passed, 4 skipped, 1 xfailed |
| Scan one CI01_01 recording (371 MB) over HTTPS with `scan_frames` | 23 s; all 249 chunks match the chunk table the published store was built from |
| The same recording through `ingest.py scan` | Identical chunk table and start time |
| Build CI01_01 from its saved chunk table | 355 recordings, 175,314 chunks, 1,062.5 h; 2,008 detections in 8 labels; 2,617,447 temperature samples; 17 s in all |
| Read back four recordings (first, a third, two thirds, last) at head, middle and tail, against libsndfile | 12 of 12 windows byte-identical, 7 s |
| Resolve every detection to an audio sample from the store alone | 1,986 resolved, 22 refused as falling between recordings, the same as the published store |
| Open with `xr.open_datatree` (xarray 2026.9.0, zarr 3.4.1) | Opens in 0.2 s |

The calibration offset was not measured. It reads eight hours of audio, about
2 GB of FLAC, and the local runs skipped it.

The other seven deployments were not rebuilt locally.

## Judgement calls

1. **Frames per chunk.** The repository rule is to match the source's storage
   unit, which for FLAC is one frame. The published store merges 512 frames
   per chunk, 2,097,152 samples at CI01_01's blocksize of 4096. At one frame
   per chunk, CI01_01 would carry about 90 million references instead of
   175,314, and an hour of 96 kHz audio would span 84,375 chunks instead of
   165. How much that slows a read has not been measured. **Recommended:** keep 512 for the rebuild, since it reproduces
   the store that was verified, and time an hour's read at one frame per chunk
   before changing it. `ingest.FRAMES_PER_CHUNK` holds the value.
2. **The calibration offset.** `measure_counts_offset` was ported unchanged,
   and has not run since the port.
   **Recommended:** run it on Coiled during the rebuild, and compare each
   deployment's `counts_to_upa_db` with the published value.
3. **The saved chunk tables.** Each deployment's frame scan is saved as a
   1 to 2 MB `.npz`, which lets a rebuild skip reading the audio. The scan of
   CI01_01 alone reads 272 GB. The tables are data, so they are not committed.
   They currently exist only on one laptop. **Recommended:** upload them to the
   repo's bucket prefix beside the store.

These calls were made in the port and need no decision:

- **Code shared with other archives went to `minus80`.** The FLAC scan, the
  chunk table and the array writer were already in `minus80.parsers.flac`. The
  `gs://` virtual chunk container, with anonymous credentials, was added to
  `minus80.core.icechunk_io` for this ingest.
- **The waveform has no channel axis.** Every recording is mono, so the
  ingest passes `channel_axis=False` to the shared `write_array`, which then
  writes `("time",)`, the published store's shape. The parser's default stays
  `("time", "channel")`, and it refuses `channel_axis=False` for a table with
  more than one channel.
- **The detection selftest reads only the segment table.** It checks each
  detection time against the recording bounds, and resolves the same 1,986 of
  2,008 detections as the earlier version, which went through an xarray index.
- **The store's text names the codec by tag.** It points to
  `zarr3-flac-codec-2026-10-19`, the tag minus-80 pins, rather than to the
  branch. It also names xarray 2026.09.0 as the first release that opens the
  store.

## Steps, scope and risk

| Step | Writes | Needs approval |
|---|---|---|
| Merge the port | Nothing outside this repository | This review |
| Upload the saved chunk tables | The repo's bucket prefix | Yes |
| Rebuild all eight deployments on Coiled, from the chunk tables, to a new branch of `earthmover-biology/sanctsound` | A branch; `main` is untouched | Yes, and an API-client grant on the repo |
| Verify the branch with `verify.py` for each deployment | Nothing | |
| Check the bio-demos notebooks against the branch, then move `main` to it | `main` | Yes |

These steps changed when they ran. Each rebuilt deployment was committed
straight to `main`, which the maintainer approved, and only the three partial
deployments were rebuilt. The chunk tables were not uploaded, and the bio-demos
notebooks have not been run against the rebuilt groups.
[REPORT.md](REPORT.md), "Changes from the plan", has the details.

Out of scope:

- adding deployments beyond the published eight;
- the filename forms the code cannot parse yet;
- moving the detections to an Iceberg table, as the repository rules ask for
  tabular data with no Zarr convention.

The main risk is that a reader breaks without warning. The rebuild keeps the
waveform's shape but changes some attribute text, so it stays on a branch until
the bio-demos notebooks have run against it.
