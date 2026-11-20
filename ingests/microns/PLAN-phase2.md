# MICrONS minnie, phase 2: processed two-photon data and EM matches by CAVE version

## Summary

Phase 2 adds three things to each scan group of the phase-1 store,
`two-photon/<session>-<scan>`, and changes none of phase 1's arrays:

- **`nwb/`**: the scan's DANDI 000402 NWB file, whole, in the unified NWB
  Zarr v3 convention that hdmf-zarr 0.14 reads. Every chunk is a reference
  into the file at DANDI: the fluorescence traces, the ROI masks and summary
  images, the eye, pupil and treadmill recordings, the stimulus movie, the
  trial tables, and the field movies again.
- **`rois-<n>`**: field n's ROI masks, written natively as an OME-Zarr image,
  one chunk per ROI, so one ROI's mask is one read.
- **`units/`**: the scan's cells as CAVE numbers them, with the field and ROI
  each one is, and under `units/em/<table>` the EM cell each CAVE match table
  gives.

The matches are the part of MICrONS that changes. Proofreading edits the EM
segmentation, and each edit gives the edited cell a new ID, so every CAVE
version re-states every match with its current IDs. Eight versions have
public dumps of the match tables; between consecutive versions, 1.4 to 12.3%
of a table's root IDs change. Each version is a commit with a tag
(`cave-v943`), so a reader can ask which EM cell a two-photon ROI was matched
to as of any version, and Icechunk's diff between two tags lists the arrays
that changed.

**Asked of the reviewer:** approve the layout calls marked open below, the
run, and the Arraylake call under "Steps". Nothing has been written outside
local trial repos.

| # | Decision | Status |
|---|---|---|
| 1 | Keep the OME-Zarr 0.6 scenes for the movies, and add each scan's NWB file whole in the unified NWB Zarr v3 convention | Decided |
| 2 | Put the NWB file at `two-photon/<scan>/nwb`, inside the scan group, rather than in a sibling tree | Open; recommended |
| 3 | Keep the EM matches in the store, on a per-scan `units` axis, one commit and tag per CAVE version; the full CAVE tables go to Iceberg later | Decided |
| 4 | Write the ROI masks natively, one image per field with one chunk per ROI, not as a label image | Natively: decided. The layout is open; recommended, because 57% of the pixels the ROIs cover belong to two or more ROIs |
| 5 | Place the mask images with a translation of their own, outside the scene | Open; recommended: a scene requires its images to share its axes, and the ROI axis is not time |
| 6 | Derive `unit_id` by the rule in "What has been tested", and re-check it on every row each run | Decided |
| 7 | Scope: all 19 scans, the 8 CAVE versions with dumps, the stimulus movie, the trial tables | Decided; the movie and the trial tables come with the NWB file |
| 8 | Destination: `earthmover-biology/MICrONS`, with a virtual chunk container for `s3://dandiarchive/blobs/` | Decided; the container needs the call under "Steps" |

The figures are in [`diagrams/`](diagrams/), written by
[`diagrams_processed.py`](diagrams_processed.py): what phase 2 reads
([`processed-source.svg`](diagrams/processed-source.svg)), how one field's
data and the CAVE rows become store nodes
([`processed-mapping.svg`](diagrams/processed-mapping.svg)), and a scan
group after phase 2 ([`processed-store.svg`](diagrams/processed-store.svg)).
[`archive.svg`](diagrams/archive.svg) maps everything MICrONS minnie publishes, by host, with a
picture of each part and whether the store holds it.
[LAYOUTS.md](LAYOUTS.md), "Phase 2", records the evidence behind each mapping.

Phase 2 has its own plan because phase 1's [PLAN.md](PLAN.md) is the
approved record of the store as published, and each phase is approved and
reported on its own.

## The archive

[PLAN.md](PLAN.md) describes MICrONS and phase 1's scans. This section covers
every part phase 1 leaves out, ranked by what a store adds to it against what
it costs. Sizes are from listings of the buckets in November 2026. BossDB's
project page and DANDI's metadata state CC BY 4.0, and the
[MICrONS Explorer terms](https://www.microns-explorer.org/terms-and-conditions)
put the MICrONS data, the CAVE tables included, under CC BY 4.0.

| Rank | Part | Where, format | Size | Changes | Virtual | Deciding number |
|---|---|---|---|---|---|---|
| 1 | CAVE two-photon-to-EM match tables | `gs://mat_dbs/public/minnie65_phase3_v1/<version>/`, whole-file gzip CSV | 0.2k to 84k rows, 6 KB to 4.7 MB per table | every version: rows fixed, root IDs change | no: whole-file gzip; written natively | 1.4 to 12.3% of root IDs change between consecutive versions |
| 2 | NWB processed data | `s3://dandiarchive/blobs/`, us-east-2, HDF5 with gzip chunks | 168,971 ROIs in 144 fields | not since 2023 | yes | shadowzarr maps a whole file in 59 s; h5py's walk of its 364 objects takes 761 s |
| 3 | NWB stimulus movie | the same files, one gzip frame per chunk | 381,054 frames in scan 4-7 | not since 2023 | yes | 381,054 chunks per scan, which shadowzarr indexes in 29 s |
| 4 | Digital-twin properties | BossDB `functional_data/digital_twin_properties/`: v1 netCDF (HDF5), v2 uncompressed `.npy` and CSV | 33 GB and 34 GB | v1 and v2, with different units and conventions | yes | v2 `responses.npy` is (104,171, 75,000) float32, a range per unit |
| 5 | Structural stack | BossDB `two_photon_structural_stacks/`, uncompressed float32 TIFF, one strip per page | 2 files, 11 GB | no | yes | 670 and 1,340 pages; the transforms into it are only in DataJoint |
| 6 | DataJoint database | BossDB `two_photon_processed_data_and_metadata/`, MySQL dump and Docker images, v7 and v8 | 452 GB | v7, v8 | no | needs a MySQL restore before anything can be read |
| 7 | microCT | BossDB `microCT/17797_1R_0.94um`, Neuroglancer precomputed, raw 64³ chunks | 4,171 objects, 1.9 GB | rebuilt 2026-10-26 | yes | already cloud-optimized; no transform places it in EM space |
| 8 | Segmentations | BossDB `minnie65/seg` (v117); `gs://iarpa_microns/minnie/minnie65/seg_m{343,943,1300}` | 12 TB at v117 | 4 static versions | with a custom codec and shard index | about 627 million chunks at 8 nm per version |
| 9 | EM, minnie65 and minnie35 | BossDB and `gs://iarpa_microns/minnie/minnie65/em`, sharded precomputed | 117 TB and 55 TB | no | yes | about 1.9 billion chunks at 8 nm on BossDB; already cloud-optimized |
| 10 | Stimulus AVIs | BossDB `stimulus_movies/` (v3, v4, MP4) and `platinum_stimulus_movies/` | 247 GB and 42 GB | v3, v4 | per frame: each frame is a PNG | the NWB file holds the v4 movie at the same 144 × 256 size |
| 11 | Synapse tables | BossDB `synapse_graph/` and `mat_dbs` (v1718) | 65 GB; 13.8 to 22.9 GB gzip per table | per version | no | an Iceberg table, and the largest tabular part |
| 12 | Viewer copies | BossDB `field_seg_masks/`, `field_summaries/`, `functional_series/`, `stimulus_n5/`, `stack_rgb/` | small | no | yes | precomputed and N5 copies of data phase 2 references at its source |

The ranking weighs the four things a store adds:

- **Versions.** The match tables change every quarter. The segmentations
  change too, but each version is a new 627-million-chunk volume.
- **Formats that are not cloud-optimized.** The NWB files are slow to
  discover: a full walk of one file's 364 objects took 761 s and 8,049
  requests from a laptop; the trial repo opened in under 0.1 s.
- **Joins across hosts.** The matches live on Google Cloud and key cells by
  CAVE `unit_id`; the traces live on DANDI and key them by field and ROI.
- **Discovery.** The `unit_id` rule (decision 6) and the even-field coverage
  of the matches are stated nowhere; the store states both.

The EM, the segmentations and microCT are already Neuroglancer precomputed,
which a reader can range-read today, and the EM and segmentations would add
hundreds of millions of references for that. The digital twin and the
structural stack are good later phases: both virtualize, and the digital twin
has two versions.

The MICrONS tutorial reads the matches from CAVE through its Python client,
with a token, and the traces from DANDI or DataJoint.

## Proposed layout

### The tree

```
two-photon/<session>-<scan>                    ome.scene (phase 1); its scan attribute gains a "processed" entry
  field-<n>/0                                  phase 1: the TIFF pages, (t, z, y, x)
  volume_times                                 phase 1
  rois-<n>/0                                   image (roi, z, y, x) float32, written natively
  units/unit_id, field, roi                    (unit,) int64
  units/em/<table>/                            pt_root_id, pt_supervoxel_id (unit,) uint64; id, target_id (unit,) int64;
                                               pt_position (unit, 3) int64; residual, score, volume (unit,) float64
  nwb/                                         an NWBFile, as shadowzarr maps DANDI's file
    acquisition/TwoPhotonSeries<n>/            field n's movie again, (time, columns, rows), gzip
    acquisition/EyeTracking/, PupilTracking/, treadmill_velocity/
    acquisition/Video: stimulus_17797_<s>_<n>_v4/   the stimulus movie, one gzip frame per chunk
    general/optophysiology/ImagingPlane<n>/
    processing/ophys/ImageSegmentation/PlaneSegmentation<n>/   image_mask, id, mask_type, NWB EM columns
    processing/ophys/Fluorescence/RoiResponseSeries<n>/        data (time, roi), rois (roi), timestamps (time)
    processing/ophys/SegmentationImages<n>/                    average, correlation
    intervals/Clip/, Monet2/, Trippy/                          the trial tables, one array per column
    specifications/                                            NWB's schema cache
```

Scan 4-7, field 4: `rois-4/0` is (1,389, 1, 440, 248) with chunks
(1, 1, 440, 248) in shards of 261 ROIs; `nwb/processing/ophys/Fluorescence/RoiResponseSeries4/data`
is (40,000, 1,389) with the NWB's chunks (625, 44); `units` has 8,395
entries, and ROI 23 of field 4 is unit 2,573.

### Where the NWB file sits, and what it shares with the scene

The NWB file goes inside the scan group, at `nwb/`, so one group holds
everything about a scan, and the scan's attributes say where each part is.
A sibling tree, `processed/<scan>/`, would split each scan across two places
for no gain: pynwb cannot open a file below the store's root either way
(docs/formats.md).

The file and the scene describe the same scan, and each keeps one meaning:

- **The frames.** `field-<n>` is the movie in the TIFF's orientation, one
  chunk per frame, placed in the microscope's space. The NWB's
  `TwoPhotonSeries<n>` holds the same values, transposed, in chunks that suit
  one pixel's history. The store keeps both, because the NWB file is written
  whole, and the scan's `processed` attribute says they are the same frames.
- **The masks.** `rois-<n>` is the derived, reader-friendly copy, in the
  field's orientation, placed where the field is; its attributes name the
  NWB table it comes from. The NWB's `image_mask` is the published original.
- **The cells.** `units` is the only place that joins the NWB's per-field
  ROI ids to CAVE's per-scan `unit_id`; it holds no data the NWB file has.
- **Everything else** (traces, summary images, behavior, stimulus, trial
  tables, the NWB's own v343 EM columns) is only in `nwb/`. The store adds
  `dimension_names` to every array there, as the unified convention allows,
  so `xr.open_zarr` opens any series or table group: a RoiResponseSeries'
  `data` and `rois` share a `roi` axis, its `data` and `timestamps` a `time`
  axis. The shared `name_dimensions` names a table's row axis after the
  table, `PlaneSegmentation<n>`; the ingest renames it `roi` once it has
  checked that the traces' `rois` region lists every row of the table in
  order, so a field's masks, EM columns and traces align. The rename stays in
  the ingest because a region in general selects rows in another order: an
  IBL ElectricalSeries' `electrodes` are one probe's rows of a two-probe
  table.

### The masks

The NWB stores each field's masks as (rois, columns, rows) float32, gzip,
in chunks of 87 ROIs by 16 × 28 pixels, so one ROI's mask costs 256 chunk
reads. Written natively, the masks of scan 4-7 take 4.8 MB with Blosc zstd
and byte shuffle (8,395 ROIs, 3.66 GB uncompressed; DANDI stores them in
9.2 MB of gzip), so all 19 scans take about 100 MB. `rois-<n>` holds them in
the TIFF's orientation, (roi, z, y, x), with one chunk per ROI inside
shards of 261 ROIs, three of the NWB's chunks. A reader fetches one ROI's
mask with one read, and the store holds about 700 mask objects instead of
168,971. The ingest reads and writes a shard at a time: on scan 7-4, the
scan with the most ROIs (12,675; 5.5 GB uncompressed), the whole scan's
write peaked at 0.65 GiB of memory, against 3.1 GiB a field at a time and
5.8 GiB for the scan at once.

A label image would be smaller (0.16 MB for scan 4-7) but lossy: the masks
are weights between 0.01 and 3.4, not 0/1, and of the 653,578 pixels any ROI
in scan 4-7 covers, 374,274 are covered by two or more ROIs. OME-Zarr label
images hold one integer per pixel.

The ROI axis has the custom type `roi`, which OME-Zarr allows once per image.
The image places itself with a translation equal to its field's place in the
scene, because `minus80.ome.scene_attrs` and the 0.6 scene require every
image of a scene to share the scene's axes (t, z, y, x), and the ROI axis is
not time.

### The units and the EM matches

`units` holds `unit_id`, `field` and `roi` for every cell of the scan. Each
CAVE match table becomes `units/em/<table>`, one array per column on the
`unit` axis; a unit the table does not match holds the fill value (0 for root
and supervoxel IDs, −1 for other integers and the EM point, NaN for floats).
A scan the table has no rows for gets no group: v343's
`func_unit_em_match_release` covers 13 of the 19 scans, and the manual
tables 16. Each CAVE version is a commit: the tables it lists are written in place, so
an array whose values did not change is not rewritten, and a table it no
longer lists is deleted. Root and supervoxel IDs are uint64 and exact; the
NWB's own copies are float64 and lose the last two or three digits.

### Referenced and written

| Product | How |
|---|---|
| The NWB file: every dataset of 1,000 elements or more | referenced, with the NWB's own chunks and gzip (numcodecs `zlib`) |
| The NWB file: strings, scalars, small arrays | inline in the store, as shadowzarr maps them |
| ROI masks, `rois-<n>` | written natively, Blosc zstd with byte shuffle, sharded |
| `units` and the EM matches | written natively, Blosc zstd with byte shuffle, one chunk per array |
| Full CAVE tables, including rows for scans with no NWB file | later, as Iceberg tables beside the store |

Every codec in the store is one zarr-python decodes alone: `bytes`,
`transpose`, `sharding_indexed`, `crc32c`, `blosc`, `zstd`, `vlen-utf8` and
numcodecs' `zlib`. No array uses shadowzarr's `keep_bytes`; the verify checks
both at every phase-2 commit.

### Layouts considered and rejected

- **Hand-built groups for traces, behavior and stimulus**, written from the
  NWB's datasets as `traces-<n>`, `behavior` and `stimulus`. The whole NWB file keeps NWB's paths, types and links, which a
  reader of any other NWB file already knows, and its arrays open in xarray
  once named; separate groups would say the same thing twice.
- **The NWB file in a sibling tree, `processed/<scan>/`.** See above.
- **The masks only in `nwb/`.** One mask costs 256 chunk reads there; 2.3 s
  from the laptop against a local read for `rois-<n>`.
- **The masks as a label image.** Lossy, as above.
- **One object per ROI.** 168,971 objects of about 600 bytes each; a shard
  keeps the one-read access in about 700 objects.
- **One shard per field.** One object per field, but writing it needs the
  field's masks in memory at once, up to 0.7 GB, and peaked at 3.1 GiB on
  scan 7-4.
- **The matches only in Iceberg.** Decided against: a tag would not pin the
  matches with the traces.
- **A summary-image OME-Zarr copy, `summary-<n>`.** The NWB's two summary
  images per field open from `nwb/` directly; a copy placed in the scene
  would repeat 14 MB per scan, uncompressed, of float64.

## What has been tested

All on a laptop, reading the buckets over the internet, on 2026-11-20. The
trial built phase 1 for scan 4-7 into a local repo (54 s, verified and
tagged `v1`, `v2`), then ran `processed.py` and `verify_processed.py` as the
run will.

**The run, scan 4-7.**

| Step | Time | Result |
|---|---|---|
| read and write the masks, then map the NWB file with shadowzarr | 65 s, 53 s of it the mapping | 8,395 ROIs in 8 images; 276 NWB arrays, 2,610 keys; the stimulus movie's 381,054-chunk index takes about 29 s |
| the processed commit | 83 s for this and the eight CAVE commits | 287 new arrays, 98 new groups; `/` and `two-photon/4-7` updated |
| eight CAVE commits | 1 to 3 s each | 8, 18, 2, 34, 5, 5, 20 and 4 arrays written |
| `verify_processed.py --tag` | 30 s | every check passes; `processed` and eight `cave-v<version>` tags |
| phase 1's `verify.py`, after phase 2 | 9 s | passes |

The repo holding both phases of scan 4-7 is 25 MB. A rerun of
`processed.py` writes nothing and finds every commit in 3 s.

**All 19 scans against the dumps.** `unit_rows` and `match_arrays` ran
over every scan and every dumped version, from the cached dumps and the 19
NWB files' ROI counts, in 1 s, with no error: 28 to 89 table groups per
version. The tables leave out scans as follows: v343's
`func_unit_em_match_release` has no rows for 4-9, 4-10, 5-3, 8-7, 8-9 and
9-6; `functional_coreg` none for 4-9, 4-10, 8-7 and 8-9; the manual tables
none for 4-9, 4-10 and 8-9.

**Memory.** Scan 7-4 written alone: 0.65 GiB peak (above). Eight workers
at once therefore need about 5 GiB. Merging 19 forks into the driver was
measured on a stand-in: 19 forks of 500,000 virtual references each (one
381,054-chunk array, eight of 10,368 and eight of 4,096, the shape of one
scan's NWB file) merged in 0.9 s and committed to a local repo in 4.7 s,
with the driver at 5.7 GiB peak, writing the forks included. The VM has
32 GiB.

**What the verify checks.** `complete`: the NWB group is an NWBFile, and
each field's ROI count agrees across the NWB table, `rois-<n>` and `units`.
`codecs`: every codec chain at every phase-2 commit is on the allowlist
above; scan 4-7's chains are `bytes`, `bytes`+`zlib`, `bytes`+`zstd`,
`vlen-utf8`, `vlen-utf8`+`zstd`, `blosc`+`bytes` and the sharded masks'
`sharding_indexed`+`blosc`+`crc32c`. `images`: the mask images validate as
OME-Zarr 0.6 and sit where the scene puts their field. `sources`: one mask,
one trace, one summary image and one stimulus frame equal h5py's decode of
the NWB file, and xarray opens the trace's series. `matches`: at each CAVE
commit, 100 sampled matches of each table equal the dump's root ID,
supervoxel and EM point, reading each array once per table and scan, and
each scan holds exactly the dump's tables that have rows for it.

**Reading.** Through the local trial repo: one ROI's mask from `rois-4` in
under 0.01 s, the same mask from `nwb/` in 2.3 s, and one ROI's whole trace
from `nwb/` in 1.2 s.

**The diffs.** Between `cave-v661` and `cave-v795` two chunks change, the
root IDs of the version's two tables; between `cave-v1412` and `cave-v1507`
four. Between `cave-v943` and `cave-v1412`, 271 of scan 4-7's 1,322 manual
matches have a different root ID; ROI 23 of field 4 holds
864691135719446577 at v943.

**The `unit_id` rule.** In all 19 NWB files, every field's ROI ids run
1 to n. With `unit_id` = (ROIs in fields 1 to n − 1) + ROI id, every row of
every match table that has a `field` column puts its unit in that field:
26 table versions, 0 disagreements. In scan 4-7, all 687 rows of v343's
`functional_coreg` give the same EM point as the NWB columns of the ROI the
rule names. The run raises on any row that breaks the rule.

**What the tables cover.**

| Table | Versions | Rows | Scans | Rows for scans with no NWB |
|---|---|---|---|---|
| `func_unit_em_match_release` | v343 | 200 | 13 | 0 |
| `functional_coreg` | v343 | 9,518 (9,434 distinct) | 15 | 0 |
| `apl_functional_coreg_forward_v5` | v661, v795 | 68,873 | 19 | 0 |
| `coregistration_manual_v3` | v661 to v1078 | 13,925 | 16 | 0 |
| `coregistration_manual_v4` | v943 to v1507 | 19,181 | 16 | 0 |
| `apl_functional_coreg_vess_fwd` | v943 to v1507 | 75,856 | 22 | 11,210 |
| `coregistration_auto_phase3_fwd` | v943 to v1181 | 84,198 | 22 | 12,210 |
| `coregistration_auto_phase3_fwd_apl_vess_combined` | v943 to v1181 | 84,233 | 22 | 12,213 |
| `coregistration_auto_phase3_fwd_v2` | v1412, v1507 | 82,181 | 22 | 11,930 |
| `coregistration_auto_phase3_fwd_apl_vess_combined_v2` | v1412, v1507 | 83,046 | 22 | 12,043 |

The extra scans of the automatic tables are 5-4, 5-5 and 8-4, published only
in `platinum_camovies/`. Their rows cannot join an NWB file; each CAVE
commit's metadata counts them, and the Iceberg tables will keep them.

**The fixture test.** `test_processed.py` runs phase 2 on fixture.py's small
scan, with its NWB file extended by overlapping masks and traces, and two
hand-made CAVE dumps, and a second scan that one table leaves out: every mask
equals the NWB's, the NWB file reads back whole with `roi` axes, each version
holds its own exact root IDs, a scan gets only the tables with rows for it, a
version's diff touches only the root IDs that changed and deletes a dropped
table, a repeated row is kept once, and a row that breaks the unit rule or a
unit with two different rows is refused.

## Judgement calls

1. **Layout: both.** Decided: the scenes stay, and each NWB file is written
   whole as shadowzarr maps it, with `dimension_names` added and nothing
   else, as in the IBL ingest.
2. **The NWB file inside the scan group.** One group per scan, and the scan's
   attributes name each part. **Recommended:** `two-photon/<scan>/nwb`.
3. **Matches in the store.** Decided, with tags `cave-v<version>`.
4. **Mask layout.** Decided to write them natively. **Recommended:** a
   weight image per field, one chunk per ROI in shards of 261 ROIs, because a
   label image would drop most overlapping pixels.
5. **Mask placement.** **Recommended:** the image's own translation, equal to
   its field's place in the scene; listed in no scene, and its attributes
   name its field.
6. **The `unit_id` rule.** Decided: derived, stated in the attributes of
   `units`, and re-checked on every row each run.
7. **Duplicates.** v343's `functional_coreg` has 84 rows that repeat another
   in everything but `id`; one of each is kept, and the commit's metadata
   counts the rest.
8. **Destination.** Decided: the existing repo.

## Steps, scope and risk

**Scope.** In: the 19 NWB files of DANDI 000402 version 0.230307.2132,
whole; the masks; the match tables of v343, v661, v795, v943, v1078, v1181,
v1412 and v1507. Out: the other parts in the ranking, the CAVE versions
without a dump (v1300's dump has no match tables; later versions need a CAVE
token), and the full CAVE tables.

**Steps.**

1. Approval of this plan and the open calls (2, 4, 5).
2. Ian adds the repo's virtual chunk container (below). *Changes the repo.*
3. Run 3, [`runs/03_processed.sh`](runs/03_processed.sh): one Coiled VM
   (`c6i.4xlarge`) in us-east-2, beside DANDI, with `coiled batch run`. It
   writes the processed commit for all 19 scans, 8 at a time, then one commit
   per CAVE version, then runs `verify_processed.py --tag`. The launcher
   refuses uncommitted or unpushed code and tags the code's commit
   `microns-run3-processed`. A relaunch from the same commit reuses the tag
   and resumes, and one from another commit is refused (both checked against
   a stand-in remote with `coiled` stubbed out). The relaunched job skips
   every commit main already holds. *Writes the store.*
4. REPORT.md, with the snapshots, timings and verify output.

**Arraylake setup.** The `dandiarchive` bucket config (us-east-2) and its
public access policy on `blobs/` exist in `earthmover-biology`. The repo
needs its own container for the prefix, from a login with write access to
the repo, after approval:

```python
from arraylake import Client

c = Client()
print(c.get_virtual_chunk_containers("earthmover-biology/MICrONS"))
c.update_virtual_chunk_containers("earthmover-biology/MICrONS",
                                  add={"s3://dandiarchive/blobs/": "dandiarchive"})
```

`update_virtual_chunk_containers` leaves the existing BossDB container as it
is. `processed.py` refuses to start if the container is missing.

**Expected run.** About 2 minutes to install. The processed commit: 19
scans at about 70 s each from the laptop, 8 at a time, about 3 minutes, plus
the commit. The CAVE commits: about 10 MB of dumps per version from Google
Cloud and a few hundred small arrays each, under a minute each. The verify:
about 2 minutes, most of it fetching the dumps again. Hard limits of 90
minutes for the ingest and 20 for the verify.

**Cost.** Under $2: the VM for under an hour (about $0.70), about 160 MB of
egress from Google Cloud (the dumps, read twice; about 2 cents), reads from
DANDI's bucket inside us-east-2, and about 100 MB of masks and the manifests
in the repo.

**Risks.**

- *Commit size.* The NWB files add about 500,000 references per scan
  (381,054 stimulus frames, 82,944 field-movie chunks, the masks and the
  traces), about 10 million in all if the other scans are like 4-7, beside
  phase 1's 11.8 million. The commit against Arraylake is unmeasured.
- *The convention is new.* hdmf-zarr 0.14 is two months old and shadowzarr
  has no release; the workspace pins a commit. pynwb cannot open the NWB
  groups until hdmf-zarr accepts a group (docs/upstream.md); zarr and xarray
  read them now.
- *DANDI's blob URLs.* The references name DANDI's content-addressed blobs
  of version 0.230307.2132 and carry their ETags, so a changed object fails
  a read instead of returning other bytes.
- *Future CAVE versions.* v1926 is current and has no dump. Adding it means
  a CAVE token and one more commit; the layout takes it unchanged.

## Later phases

- The digital-twin properties, v1 and v2 as two commits, both virtual.
- The structural stack, once the per-field registrations are read from the
  DataJoint dump.
- The full CAVE tables and the synapse tables as Iceberg tables.
- EM and segmentations, if a reader needs them through Icechunk rather than
  as Neuroglancer precomputed.
