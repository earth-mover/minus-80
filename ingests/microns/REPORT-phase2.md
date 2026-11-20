# MICrONS minnie, phase 2: report

Phase 2 is in the Arraylake repo `earthmover-biology/MICrONS`, on `main` at snapshot `WPVBN1EHGEFGZSA1FPQ0`. It added three things to each of the 19 two-photon scans:

- the scan's DANDI 000402 NWB file, whole, with its processed data;
- each field's cell outlines;
- the scan's cells, matched to cells in the electron-microscopy volume, as each of eight CAVE releases states the match.

[PLAN-phase2.md](PLAN-phase2.md) is the plan it followed, and [LAYOUTS.md](LAYOUTS.md), "Phase 2", records what the sources taught. Phase 1's runs are recorded in [`runs/01_twophoton.sh`](runs/01_twophoton.sh) and [`runs/02_scenes.sh`](runs/02_scenes.sh).

## What was built

| Node, in each `two-photon/<session>-<scan>` | What it is | Count |
|---|---|---|
| `field-<n>` (phase 1) | the calcium-imaging movie of field n | 144 |
| `rois-<n>` | field n's cell outlines, `(roi, z, y, x)` float32, one chunk per cell in shards of 261 | 144 images, 168,971 cells |
| `units` | the scan's cells as CAVE numbers them, with each one's field and ROI | 19 |
| `units/em/<table>` | each cell's matched EM cell in one CAVE match table | 73 at `main` |
| `nwb` | the scan's NWB file: traces, outlines, summary images, behavior, stimulus movie, trial tables, and the movies again | 19 files, 4,936 arrays |

| Tag | Snapshot | What it holds |
|---|---|---|
| `v1`, `v2` | `KV69Y4ABGKVQSQKZBDGG`, `PZK7W7CJ181GTDPC1JFG` | phase 1: the two releases of the movies |
| `processed` | `9WSAVP0Y5CW36T6RWYHG` | the NWB files, outlines and unit axes, with the matches the NWB files carry (CAVE release 343) |
| `cave-v343` | `285YSY0EKJ76F0GS5B00` | 2 tables, 112 arrays written |
| `cave-v661` | `1KB357RG33PM8DZZD51G` | 2 tables, 308 |
| `cave-v795` | `RE40KPQ44VT3FVZDEXKG` | 2 tables, 34 |
| `cave-v943` | `6Q1MP8C8M8QKHET7TH4G` | 5 tables, 618 |
| `cave-v1078` | `VHHM772E04MC2PG85BX0` | 5 tables, 87 |
| `cave-v1181` | `T89VWF6FZJNQA6V2V39G` | 4 tables, 89 |
| `cave-v1412` | `1BK2EBYDJ4KK0ZWCF0DG` | 4 tables, 376 |
| `cave-v1507` | `WPVBN1EHGEFGZSA1FPQ0` (`main`) | 4 tables, 72 |

"Arrays written" counts arrays added, arrays rewritten because their values changed, and table groups deleted. A release that only restates existing tables writes few: 34 arrays across 19 scans from 661 to 795, and 72 from 1412 to 1507.

The run also restated, as metadata only, the root, the `two-photon` group, every scan's description and every field image's description, so they say what the data is. The root gained `provenance` (how the store holds it) and `license_terms` (CC BY 4.0, the MICrONS terms, which cover the CAVE tables).

[`microns.ipynb`](microns.ipynb) opens the store. It needs zarr, xarray, icechunk and matplotlib, and an Arraylake login with read access.

## The run

Run 3, [`runs/03_processed.sh`](runs/03_processed.sh), on 2026-11-20: one Coiled c6i.4xlarge in us-east-2, beside DANDI's bucket, from commit `dd82e26`, tagged `microns-run3-processed`.

| Step | Time |
|---|---|
| install | 6 s |
| the processed commit: 19 scans, 8 at a time, 19 forks merged in 1.9 s | about 5 min |
| eight CAVE commits | 1,131 s for the whole ingest, the processed commit included |
| `verify_processed.py --tag` | 331 s |
| job, start to end | 25 min 6 s |

Peak memory was about 8.7 GB during the ingest and 2 GB during verify, on a 32 GiB VM. The plan expected under a minute per CAVE commit; against Arraylake they took 1 to 3 minutes each.

## Verification

`verify_processed.py` passed every check at every phase-2 commit, then created the nine tags:

- `complete`: every scan has its NWB group, an NWBFile, and one `rois-<n>` per field whose cell count equals the NWB table's and `units`'.
- `codecs`: every array at every phase-2 commit uses only codecs zarr-python decodes with nothing else installed.
- `images`: the outline images validate as OME-Zarr 0.6 and sit where the scene places their field.
- `sources`: in two scans, one outline, one trace, one summary image and one stimulus frame equal h5py's decode of the NWB file, and xarray opens the trace's series.
- `matches`: at each CAVE commit, 100 sampled matches per table equal the dump's EM cell ID, supervoxel and EM point, and each scan holds exactly the tables with rows for it.
- `versions`: the processed commit and all eight CAVE commits are on `main`.

After the run, from a laptop:

- Phase 1's `verify.py` passed at both movie releases, and with `--branch main`, which reads the restated root and scans.
- `microns.ipynb` ran against Arraylake with no error, from a clean environment. It shows the outlines, ROI 23's trace, and unit 2621, whose matched EM cell changes between `cave-v943` and `cave-v1412`.

## Changes from the plan

- **Outline shards.** The plan had one shard per field. A field's outlines are up to 0.7 GB uncompressed, so they are written 261 cells at a time. Scan 7-4 then peaked at 0.65 GiB on a laptop, against 3.1 GiB a field at a time.
- **Tables a scan lacks.** A scan gets an `em/<table>` group only for tables with rows for it. v343's `func_unit_em_match_release`, for example, covers 13 of the 19 scans.
- **The outlines' row axis.** Each field's PlaneSegmentation rows are named `roi`, the axis of its traces, after the ingest checks that the traces list every row in order. The shared `name_dimensions` leaves this to the ingest, because elsewhere a region selects other rows.
- **Wording.** Descriptions and commit messages say what the data is; storage details are in `layout` and `provenance`.
- **Relaunch.** The launcher reuses its tag when it names the same commit, so a stopped run can resume.

## Judgement calls made

- The NWB file sits inside each scan group, at `nwb/`.
- The outlines are written natively as weight images, not a label image: 57% of the pixels any outline covers belong to two or more.
- The outline images carry their field's translation themselves and are not listed in the scene, because a scene's images share its (t, z, y, x) axes.
- `unit_id` is derived from the ROI counts per field. Every match row that names a field agreed with the rule, and the run checked it again.
- v343's `functional_coreg` kept one of each of its 84 repeated rows.
- Rows for scans 5-4, 5-5 and 8-4, which have no NWB file, are counted in each CAVE commit's metadata and left out of the store.

## Problems found in the source data

- The NWB files store EM cell IDs as float64, which rounds an 18-digit ID; `units/em` holds them exactly.
- No file states how the NWB's per-field ROI ids relate to CAVE's per-scan `unit_id`; LAYOUTS.md records the rule and its evidence.
- v343's `functional_coreg` dump repeats 84 rows in everything but `id`.
- The automatic match tables from v943 on hold 11,210 to 12,213 rows per table for scans 5-4, 5-5 and 8-4. Those scans are published only in `platinum_camovies/` and have no NWB file.
- CAVE release 1300's dump has no match tables, and releases after 1507 have no static dump.

## Upstream issues hit

All in [docs/upstream.md](../../docs/upstream.md):

- shadowzarr writes a reference file system only at a session's root (worked around by `under_group`) and writes no `dimension_names` (`name_dimensions`).
- `NWBZarrIO` opens only a store's root, so pynwb cannot open an NWB file kept at a group until hdmf-zarr accepts one. zarr and xarray read it now.
- shadowzarr has no release; the workspace pins a commit.
