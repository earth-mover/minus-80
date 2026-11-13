# SanctSound: ingest report

## What was built

SanctSound's hydrophone recordings are in the Arraylake repo
`earthmover-biology/sanctsound`. Each deployment is a group of chunk
references into NOAA's public bucket, `gs://noaa-passive-bioacoustic`, with
NOAA's detections, hourly sound levels, temperature record and calibration
written beside the audio. No audio was copied.

- **Snapshot.** `8TMM27Z42J3YBQ32HH40` on `main`.
- **Contents.** Eight of the archive's 196 deployments, each in full:

  | Deployment | Recordings | Hours | Built by |
  |---|---|---|---|
  | CI01_01 | 355 | 1,062.5 | the published store, September 2026 |
  | CI01_02 | 527 | 3,144.6 | the published store |
  | CI02_01 | 590 | 1,767.5 | run 2 |
  | CI04_01 | 469 | 1,405.9 | run 1 |
  | CI05_01 | 755 | 2,264.0 | run 2 |
  | OC01_01 | 525 | 3,148.4 | the published store |
  | OC01_03 | 642 | 3,841.1 | the published store |
  | SB01_03 | 316 | 1,260.1 | the published store |

  The published store held CI02_01, CI04_01 and CI05_01 only for the
  recordings around four events in November 2018. Runs 1 and 2 replaced each
  of those groups with the whole deployment.
- **Layout.** One group per deployment, as PLAN.md and LAYOUTS.md describe.
- **Runs.** Each run is a script under `runs/` that names the commit it ran
  from and the snapshot it produced. Both ran on Coiled in us-east-1, the AWS
  region nearest the bucket.

  | Run | Deployments | VM | Time | Scan rate | Snapshots |
  |---|---|---|---|---|---|
  | [1](runs/01_ci04_01.sh), 2026-11-09 | CI04_01 (216.7 GB) | c6in.2xlarge | 13.5 min | about 330 MB/s | `RZ3E76HBREW57HX18PS0` |
  | [2](runs/02_ci02_01_ci05_01.sh), 2026-11-10 | CI02_01 (305.3 GB), CI05_01 (369.0 GB) | c6in.4xlarge | 10 min 50 s | about 1.95 GB/s | `FTNXNZZBFPZNAK1ETBVG`, `8TMM27Z42J3YBQ32HH40` |

  Run 2's Icechunk commits record the minus-80 commit and the run script in
  their metadata. Run 1 predates that, so its script is the only record.
- **Opening it.** `xr.open_datatree` opens the store with xarray 2026.9.0 or
  later and the `flac` codec from flac-numcodecs. Zarr needs
  `array.rectilinear_chunks` enabled to read the waveform.

## Verification

`verify.py` ran inside each run, after each build. For each deployment it
decodes four recordings (first, a third, two thirds and last) at their head,
middle and tail, 200,000 samples each, and compares the store's samples with
libsndfile's decode of the FLAC file.

- **Samples.** All 12 windows were identical for CI04_01, CI02_01 and
  CI05_01.
- **Detections.** After each build, a selftest resolves every detection in
  every deployment to an audio sample, from the store alone. A detection that
  falls between two recordings is refused. The counts after run 2:

  | Deployment | Detections | Resolved | Refused |
  |---|---|---|---|
  | CI01_01 | 2,008 | 1,986 | 22 |
  | CI01_02 | 3,417 | 3,409 | 8 |
  | CI02_01 | 3,094 | 3,089 | 5 |
  | CI04_01 | 7,913 | 7,885 | 28 |
  | CI05_01 | 1,524 | 1,303 | 221 |
  | OC01_01 | 2,369 | 2,365 | 4 |
  | OC01_03 | 1,869 | 1,708 | 161 |
  | SB01_03 | 2,720 | 2,477 | 243 |

- **Calibration.** Each build measured the offset from counts to sound
  pressure against NOAA's hourly broadband levels. CI04_01 came out at
  85.391 dB, CI02_01 at 86.291 dB and CI05_01 at 85.891 dB, each equal to the
  value the published store held.

## Changes from the plan

- **Writes go to `main`.** The plan rebuilt all eight deployments on a new
  branch and moved `main` only after the bio-demos notebooks had run against
  it. The maintainer approved committing each deployment to `main` instead. Each build
  replaces one deployment's group, and the other groups are untouched. The
  bio-demos notebooks have not been run against the rebuilt groups yet.
- **Three deployments rebuilt, not eight.** Only the three partial
  deployments were rebuilt. The five full ones are as the published store
  built them.
- **The scan downloads, then parses.** The ported scan parsed each recording
  through ranged reads, one request at a time, at about 45 MB/s per process.
  The scan now downloads each recording with parallel range requests into
  memory and parses it there, in spawned processes. docs/formats.md, "Budget
  CPU for the download as well as the parse", has the CPU cost behind the
  choice of VM.
- **Run scripts.** The plan had no record of how each write was launched.
  `coiled/launch.sh` refuses uncommitted code, and each batch is a numbered
  script under `runs/`.

## Judgement calls made

| # | Call | As built |
|---|---|---|
| 1 | Frames per chunk | 512, as in the published store. One frame per chunk has not been measured |
| 2 | The calibration offset | Measured on Coiled in every build; all three match the published values |
| 3 | Where the saved chunk tables live | Not decided. The tables from runs 1 and 2 existed only on the VMs and are gone, so a rebuild rescans the audio, about 3 minutes per deployment on a c6in.4xlarge |

## Problems found in the source data

LAYOUTS.md, "Problems in the source", lists eight. The one that blocks more
deployments is item 8: the ancillary folders are laid out differently across
deployments. Certificates sit in nested duplicate folders or directly in
`ancillary/`, 18 deployments have no detections, and 6 have filenames in
forms the ingest does not read yet.

Calibration certificates are scanned PDFs with no text layer. Each
deployment's values are transcribed by hand into `ingest.CERTIFICATES`, so
every new deployment needs its certificate transcribed first.

## Upstream issues hit

[docs/upstream.md](../../docs/upstream.md) has the details of each.

- **Rectilinear chunk grids.** `ManifestArray` accepts only regular grids, so
  the FLAC parser writes references with Icechunk's `set_virtual_refs`
  ([VirtualiZarr#954](https://github.com/zarr-developers/VirtualiZarr/pull/954)).
- **The `flac` codec** exists only on a tag of a flac-numcodecs fork.
- **Merged range requests.** obstore's `get_ranges` merges adjacent ranges
  into one request, so `minus80.core.fetch` issues one `get_range` per block.
  Not filed.
- **PyAV.** Its Linux wheel fails on HTTPS URLs, and its demuxer slows down
  as threads are added. docs/formats.md, "Library traps", has both. No PyAV
  issue was found for either.

## Next

- The remaining 31 Channel Islands deployments and 21 Monterey Bay
  deployments, after the layout fixes in LAYOUTS.md item 8 and a decision on
  certificates nobody has transcribed.
- Where to keep the chunk tables (judgement call 3).
