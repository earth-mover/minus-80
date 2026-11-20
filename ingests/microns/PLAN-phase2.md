# MICrONS minnie, phase 2: processed two-photon data and EM matches by CAVE version

## Summary

Phase 2 adds what Baylor's processing made from the two-photon scans to the
phase-1 store, and the matches from those cells to the EM volume, one commit
per CAVE version. The processed data comes from the 19 DANDI 000402 NWB files
and stays where DANDI published it: every array is a chunk reference into the
NWB file. The data is the cell outlines (ROI masks), fluorescence traces,
summary images, eye, pupil and treadmill recordings, and the stimulus movie
the mouse watched. The matches come from CAVE's static table dumps and are
written natively, on one axis per scan of the units CAVE uses as its key.

The matches are the part of MICrONS that changes. Proofreading edits the EM
segmentation, and each edit gives the edited cell a new ID, so every CAVE
version re-states every match with its current IDs. Eight versions have
public dumps of the match tables. A table keeps its rows from version to
version; between consecutive versions, 1.4 to 12.3% of its root IDs change.
In the store, each version is a commit and a tag (`cave-v943`), so a reader
can ask which EM cell a two-photon ROI was matched to as of any version, and
Icechunk's diff between two tags lists the tables that changed.

**Asked of the reviewer:** approve the layout and the run below, and decide
the calls in the table. Nothing has been written outside a local trial repo.

| # | Decision | Recommended |
|---|---|---|
| 1 | Scope: the NWB processed data of all 19 scans, and the match tables of every CAVE version with a public dump | Yes |
| 2 | Keep the match tables in the store, on a per-scan `units` axis, rather than in an Iceberg table beside it | In the store; the full tables go to Iceberg once an Iceberg catalog exists |
| 3 | Reference the NWB stimulus movie, which adds about 7 million chunk references and most of the indexing time | Yes |
| 4 | Derive each ROI's CAVE `unit_id` by a rule no file states: the ROIs of the scan's earlier fields plus the ROI's id | Yes; every match row of every table agrees with it |
| 5 | Keep the trial tables (Clip, Monet2, Trippy) in the store as one array per column | Yes, for the same reason as decision 2 |
| 6 | Destination: the existing `earthmover-biology/MICrONS`, with a second virtual chunk container for `s3://dandiarchive/blobs/` | Yes; needs an Arraylake admin to add the bucket config and access policy |
| 7 | Reference the ROI masks with the NWB's chunking, (87, 16, 28), which makes one mask cost 256 chunk reads | Yes; a rechunked copy is a later choice |

The figures are in [`diagrams/`](diagrams/), written by
[`diagrams_processed.py`](diagrams_processed.py): what phase 2 reads
([`processed-source.svg`](diagrams/processed-source.svg)), how one field's
datasets and the CAVE rows become store arrays
([`processed-mapping.svg`](diagrams/processed-mapping.svg)), and the store
after phase 2 ([`processed-store.svg`](diagrams/processed-store.svg)).
[LAYOUTS.md](LAYOUTS.md), "Phase 2", records the evidence behind each mapping.

Phase 2 has its own plan because phase 1's [PLAN.md](PLAN.md) is the
approved record of the store as published, and each phase is approved and
reported on its own.

## The archive

[PLAN.md](PLAN.md) describes MICrONS and phase 1's scans. This section covers
every part phase 1 leaves out, ranked by what a store adds to it against what
it costs. Sizes are from listings of the buckets in November 2026; every part
is CC BY 4.0 by BossDB's project page or DANDI's metadata.

| Rank | Part | Where, format | Size | Changes | Virtual | Deciding number |
|---|---|---|---|---|---|---|
| 1 | CAVE two-photon-to-EM match tables | `gs://mat_dbs/public/minnie65_phase3_v1/<version>/`, whole-file gzip CSV | 0.2k to 84k rows, 6 KB to 4.7 MB per table | every version: rows fixed, root IDs change | no: whole-file gzip; written natively | 1.4 to 12.3% of root IDs change between consecutive versions |
| 2 | NWB processed data | `s3://dandiarchive/blobs/`, us-east-2, HDF5 with gzip chunks | 168,971 ROIs in 144 fields | not since 2023 | yes | indexing it takes 920 requests and 93 s per scan; walking the whole file takes 8,049 and 761 s |
| 3 | NWB stimulus movie | the same files, one gzip frame per chunk | 381,054 frames in scan 4-7 | not since 2023 | yes | 5,894 requests and 476 s to index one scan's frames |
| 4 | Digital-twin properties | BossDB `functional_data/digital_twin_properties/`: v1 netCDF (HDF5), v2 uncompressed `.npy` and CSV | 33 GB and 34 GB | v1 and v2, with different units and conventions | yes | v2 `responses.npy` is (104,171, 75,000) float32, a range per unit |
| 5 | Structural stack | BossDB `two_photon_structural_stacks/`, uncompressed float32 TIFF, one strip per page | 2 files, 11 GB | no | yes | 670 and 1,340 pages; the transforms into it are only in DataJoint |
| 6 | DataJoint database | BossDB `two_photon_processed_data_and_metadata/`, MySQL dump and Docker images, v7 and v8 | 452 GB | v7, v8 | no | needs a MySQL restore before anything can be read |
| 7 | microCT | BossDB `microCT/17797_1R_0.94um`, Neuroglancer precomputed, raw 64³ chunks | 4,171 objects, 1.9 GB | rebuilt 2026-10-26 | yes | already cloud-optimized; no transform places it in EM space |
| 8 | Proofread segmentations | BossDB `minnie65/seg` (v117); `gs://iarpa_microns/minnie/minnie65/seg_m{343,943,1300}` | 12 TB at v117 | 4 static versions | with a custom codec and shard index | about 627 million chunks at 8 nm per version |
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
- **Discovery.** The `unit_id` rule (decision 4) and the even-field coverage
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

Phase 2 adds nodes inside each phase-1 scan group and changes none of
phase 1's arrays.

```
two-photon/<session>-<scan>                   ome.scene: adds rois-<n> and summary-<n> beside field-<n>
two-photon/<session>-<scan>/rois-<n>          image (roi, z, y, x) float32: NWB image_mask, one weight image per ROI
two-photon/<session>-<scan>/summary-<n>       image (c, z, y, x) float64: c = average, correlation
two-photon/<session>-<scan>/traces-<n>        fluorescence (volume, roi) float32; roi, unit_id, mask_type (roi,);
                                              nwb_em/ pt_root_id, pt_supervoxel_id, pt_x/y/z_position, cave_ids as stated
two-photon/<session>-<scan>/units             unit_id, field, roi (unit,) int64
two-photon/<session>-<scan>/units/em/<table>  pt_root_id, pt_supervoxel_id (unit,) uint64; target_id (unit,) int64;
                                              pt_position (unit, 3) int64; residual, score (unit,) float64
two-photon/<session>-<scan>/behavior          eye_position (sample, 2), pupil_major_radius, pupil_minor_radius,
                                              eye_times; treadmill_velocity, treadmill_times
two-photon/<session>-<scan>/stimulus          movie (image, t c y x), times, trials/<Clip|Monet2|Trippy>/<column>
```

Scan 4-7, field 4: `rois-4/0` is (1,389, 1, 440, 248) with chunks
(87, 1, 28, 16); `traces-4/fluorescence` is (40,000, 1,389) with chunks
(625, 44); `units` has 8,395 entries, and ROI 23 of field 4 is unit 2,573.

### Axes, units, levels

- **`rois-<n>` and `summary-<n>` are images of the field.** They share the
  field movie's pixel grid, transposed back from the NWB's (columns, rows) to
  the TIFF's (rows, columns), so the scan's scene places them with the same
  translation as `field-<n>`. The ROI axis has the custom type `roi`: an
  OME-Zarr image may have one axis that is neither space nor time, and the
  masks are weights, not integer labels, so they cannot be an OME-Zarr label
  image.
- **`traces-<n>` is not an image.** Its axes are `volume`, shared with phase
  1's `volume_times` (the NWB timestamps of the traces equal those of the
  movie, checked on field 4 of scan 4-7), and `roi`. The per-ROI arrays share the `roi`
  axis, which is xarray's convention for coordinates, so `xr.open_zarr` opens
  the group as one dataset.
- **`units` is the CAVE key.** `unit_id` runs 1 to the scan's ROI count.
  Each match table becomes one group of arrays on that axis; a unit a table
  does not match holds the fill value (0 for IDs, −1 for positions, NaN for
  scores).
- **`behavior` keeps each recording's own timestamps.** Eye and pupil have
  104,227 samples and the treadmill 480,032.
- **`stimulus/movie`** is (frame, c, y, x) in monitor pixels, one chunk per
  frame, with `times` on the scan's clock. It is not placed in the scene,
  because the monitor is not in the microscope's space.
- **Levels.** One everywhere. The masks and images are the field's 440 × 248
  pixels.

### Referenced, written, beside

| Product | How |
|---|---|
| ROI masks, traces, summary images, behavior, stimulus movie and their timestamps | referenced: the NWB's own gzip chunks; masks, images and movie get a Zarr `transpose` codec |
| NWB EM columns (`pt_*`, `cave_ids`) | referenced, float64, as the NWB states them; LAYOUTS.md records that they round 18-digit IDs |
| `mask_type`, trial-table strings | written natively: HDF5 keeps variable-length strings on its heap, so they cannot be referenced |
| `unit_id`, `units/field`, `units/roi` | written natively, derived by the rule in decision 4 |
| CAVE matches | written natively, Blosc zstd with byte shuffle, one chunk per array; one commit per CAVE version |
| Full CAVE tables, including rows for scans with no NWB file | beside the store as Iceberg tables, once an Iceberg catalog exists (decision 2) |

### Layouts considered and rejected

- **Matches in an Iceberg table only.** AGENTS.md sends tabular data without
  a Zarr convention to Iceberg. These columns have one: each is a coordinate
  on the scan's `units` axis, as xarray writes them. Kept in Iceberg only,
  the versions would be Iceberg snapshots, which a reader of the store cannot
  check out with the traces, and a match would need a join on
  (session, scan, unit_id) in another system. See decision 2.
- **Matches on each field's ROI axis.** CAVE keys by `unit_id`, which spans
  the scan, and one group per scan keeps the arrays to 19 per table instead
  of 144. `traces-<n>/unit_id` gives the way back.
- **One row axis per table, keeping CAVE's rows.** Faithful to the dump, but
  a reader would look a unit up by value instead of by index. Every table
  from v661 on has exactly one row per unit; v343's `functional_coreg` has
  84 exact duplicate rows, which the ingest drops and LAYOUTS.md lists.
- **Placing the masks inside `field-<n>`.** An image group holding extra
  arrays is a node the spec does not name; phase 1 rejected `frame_times`
  there for the same reason.
- **Rebuilding the field groups around a plain `field-<n>` group.** Phase 1's
  arrays would move, and readers of the published store would break.
- **Referencing BossDB's `field_seg_masks`.** A uint64 label copy of the
  masks in precomputed `compressed_segmentation`. It needs a codec no Zarr
  reader has and loses the weights; the NWB masks are the source.
- **Referencing the stimulus AVIs.** Each frame is a PNG, which virtualizes,
  but the NWB file holds the v4 movie at the same size with gzip, a codec
  every Zarr reader has.

## What has been tested

All on a laptop, reading the buckets over the internet, on 2026-11-20.

**Indexing scan 4-7.** VirtualiZarr's `HDFParser` indexed each group through
a reader with 64 KB blocks that counted its requests.

| Group, per field × 8 | Chunks | Requests | MB read | Seconds |
|---|---|---|---|---|
| `PlaneSegmentation<n>`: image_mask, id | 2,049 to 4,097 | 23 to 44 | 1.5 to 2.9 | 1.8 to 3.3 |
| `RoiResponseSeries<n>`: data, timestamps | 1,024 to 2,080 | 32 to 53 | 2.1 to 3.5 | 2.5 to 5.1 |
| `SegmentationImages<n>` | 2 | 9 to 12 | 0.6 to 0.8 | 0.8 to 0.9 |
| behavior, 4 series | 576 | 36 | 2.4 | 2.9 |
| stimulus movie and times | 381,055 | 5,894 | 386 | 476 |
| **all** | **433,551** | **6,686** | **438** | **569** |

Without the movie, a scan indexes in about 93 s and 792 requests. The
movie's cost is its chunk count: HDF5 indexes the 381,054 frames in a B-tree
spread through the file.

**A full metadata walk.** h5py visiting every one of the file's 364 objects,
without reading any chunk index, took 761 s, 8,049 requests and 527 MB. A
reader of the store skips that walk.

**Test write.** The indexed arrays went into a local repo in one commit, in
0.4 s; the repo is 6.9 MB, almost all of it the movie's manifest.

**Reading back.** Seven reads through the repo equal h5py's own decode, byte
for byte: one ROI's whole trace, one mask (transposed), field 4's average
image (transposed), the trace timestamps against the movie's timestamps,
the eye position, 1,000 treadmill samples, and stimulus frame 200,000. One
trace plus one mask took 2.6 to 2.7 s through the repo and 10.2 to 12.1 s
through h5py with 1 MB blocks, which also read 105 index and data blocks.

**The `unit_id` rule.** In all 19 NWB files, every field's ROI ids run
1 to n. With `unit_id` = (ROIs in fields 1 to n − 1) + ROI id, every row of
every match table that has a `field` column puts its unit in that field:
26 table versions, 0 disagreements. In scan 4-7, all 687 rows of v343's
`functional_coreg` give the same EM point as the NWB columns of the ROI the
rule names, and ROI 23 of field 4 comes out as unit 2,573, the unit
CAVE's `coregistration_manual_v4` gives for its EM point.

**Commits per CAVE version.** Scan 4-7's matches went into the trial repo as
one commit per version, eight commits and eight tags. The diffs list exactly
the tables each version added or changed: v661 to v795 changes 2 arrays (the
root IDs of its two tables), v795 to v943 adds 25, v1412 to v1507 changes 4.
Between `cave-v943` and `cave-v1412`, 271 of the 1,322 manual matches of
scan 4-7 have a different root ID; ROI 23 of field 4 holds
864691135719446577 at v943, the ID the knowledge-base note found in CAVE.

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

The three extra scans of the automatic tables are 5-4, 5-5 and 8-4, the
scans published only in `platinum_camovies/`. Their rows cannot join to an
NWB file; the store leaves them out and the Iceberg tables keep them.
Matches are on the even fields: `coregistration_manual_v4` has none on fields
1, 3, 5 and 7, and the combined automatic table has 47 on field 1 and none on
the other odd fields.

## Judgement calls

1. **Scope.** The processed data and the matches are what the traces need to
   be used with the connectome, and the matches are the dataset's moving
   part. **Recommended:** both, for all 19 scans and the eight dumped
   versions: v343, v661, v795, v943, v1078, v1181, v1412 and v1507.
   v1300 and later versions have no public dump of the match tables; CAVE
   serves them to a reader with a token, which a later run can use.
2. **Matches in the store.** The store's value here is that a tag pins the
   traces and the matches together, and a diff between tags shows what a
   version changed. That needs the matches in Icechunk. The cost is a
   departure from the repository's rule for tables, argued under "Layouts
   considered"; no ingest has built an Iceberg table yet, so the full tables
   wait on that. **Recommended:** in the store, and the full tables in
   Iceberg later.
3. **The stimulus movie.** It ties each trace to what the mouse saw, at the
   time it saw it. It is 381,054 references in scan 4-7, about 7 million in
   all if the other scans' movies are as long, beside phase 1's 11.8 million, and 476 s of the 569 s it took to
   index scan 4-7. **Recommended:** reference it.
4. **The `unit_id` rule.** No file states it; DataJoint's `ScanUnit` table
   does, and the dump is 116 GB of MySQL. The rule agrees with every match
   row that names a field, and the run checks that again for every table it
   writes, and stops if one row disagrees. **Recommended:** derive it, and
   say so in the attributes of `units`.
5. **Trial tables in the store.** Clip, Monet2 and Trippy hold 384, 40 and 40
   rows in scan 4-7, and a reader needs their start and stop times beside the
   traces. **Recommended:** one array per column on a `trial` axis.
6. **Destination.** One repo per dataset (docs/ingest.md). The repo's
   container covers only the BossDB scan prefix, so it needs a second one,
   `s3://dandiarchive/blobs/` (us-east-2, anonymous), with its bucket config
   and access policy. Arraylake supports S3 containers; it also serves
   `gs://` containers, which the published SanctSound repo uses, though its
   documentation names only S3, and phase 2 needs none. **Recommended:** the
   existing repo.
7. **Mask chunking.** The NWB chunks hold 87 ROIs × 16 × 28 pixels, so one
   ROI's mask is 256 chunk reads (2.6 s on a first read from the laptop),
   while all masks of a field are 4,096 chunks. **Recommended:** reference as stored; a
   per-ROI copy can follow if readers ask for one.

Two smaller choices are recorded in LAYOUTS.md: v343's `functional_coreg`
loses its 84 exact duplicate rows, and the NWB EM columns stay float64, as
stated, beside the exact integers from the CAVE dumps.

## Steps, scope and risk

**Scope.** In: from each of the 19 NWB files, `processing/ophys`
(PlaneSegmentation, Fluorescence, SegmentationImages), the eye, pupil and
treadmill series, the stimulus movie and its times, and the `intervals`
tables; from `gs://mat_dbs`, the match tables of the eight versions above.
Out: everything else in the ranking, the CAVE versions without a dump, and
the NWB movies, which phase 1 references in the TIFFs.

**Steps.**

1. Approval of this plan and the decisions.
2. An Arraylake admin adds the bucket config and access policy for
   `s3://dandiarchive/blobs/` to `earthmover-biology` and the repo's second
   virtual chunk container. *Needs approval: changes the organization.*
3. The ingest code: `processed.py` beside `ingest.py`, with the fixture test
   and the verify extended to the new nodes, all tested on a small fixture.
4. Run 3, `runs/03_processed.sh`: one Coiled VM in us-east-2, beside DANDI,
   with `coiled batch run`. It commits the processed data of all 19 scans
   with the NWB's own EM columns, then one commit per CAVE version in version
   order, then runs the verify, which tags `processed` and `cave-v<version>`
   only if every check passes. *Needs approval: writes the store.*
5. REPORT.md, with the snapshots, timings and verify output.

**Verify.** About a minute: every scan has 8, 6 or 4 `rois`, `summary` and
`traces` groups with the NWB's ROI counts; the OME-Zarr documents validate;
for two scans, one trace, one mask, one frame and the eye position equal
h5py's decode; every match row's field agrees with the `unit_id` rule; and at
each tag, the root IDs of 100 sampled units equal the dump of that version.

**Cost.** About $2 in all:

- Indexing took 569 s for one scan from the laptop, so 19 scans take at most
  about 25 minutes with 8 at a time, if the VM is no faster; reads from
  `s3://dandiarchive` in us-east-2 are free in the region. A c6i.4xlarge for
  an hour is about $0.70.
- The CAVE dumps are 78 MB from Google Cloud, about a cent.
- The manifests are about 7 MB per scan, about 130 MB in the repo.

**Risks.**

- *Indexing on the VM.* Only the laptop was measured. HDF5's B-tree walk is
  sequential per dataset, so the VM's lower latency should shorten it, but
  the run sets a 60-minute limit and reports each scan's time.
- *Commit size.* About 8 million references join phase 1's 11.8 million.
  The trial's one-scan commit took 0.4 s locally; the commit against
  Arraylake is unmeasured. Each CAVE commit writes a few hundred small
  arrays.
- *DANDI's blob URLs.* The store references DANDI's content-addressed blobs
  of version 0.230307.2132. A new DANDI version would add blobs, not change
  these.
- *Future CAVE versions.* v1926 is current and has no dump. Adding it means a
  CAVE token and a commit per version; the layout takes it without change.

## Later phases

- The digital-twin properties, v1 and v2 as two commits, both virtual.
- The structural stack, once the per-field registrations are read from the
  DataJoint dump.
- The full CAVE tables and the synapse tables as Iceberg tables.
- EM and segmentations, if a reader needs them through Icechunk rather than
  as Neuroglancer precomputed.
