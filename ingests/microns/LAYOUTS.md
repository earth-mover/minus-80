# MICrONS two-photon scans: how the source maps onto the store

What the archive publishes, how each part becomes a node of the store, and
the choices the data did not settle. The figures in `diagrams/` draw the
same mapping: `source.svg`, `mapping.svg` and `store.svg`.

## The sources

**Pixels.** BossDB publishes the 19 functional scans of mouse 17797 under
`s3://bossdb-open-data/iarpa_microns/minnie/functional_data/two_photon_functional_scans/`,
one TIFF per scan per export:
`functional_scan_<animal>_<session>_<scan>_v<export>.tif`. There are 38
files, 1.34 TB per export, CC BY 4.0. The technical documentation in the same
directory (`..._technical_documentation_v2.pdf`, July 2023) states the scan
groups:

| Scans | Volumes | Fields | Rate | Frame | Pixel |
|---|---|---|---|---|---|
| 15 scans | 40,000 | 8 | ~6.3 Hz | 440 × 248 | 2.5 µm |
| 4-9 | 35,112 | 8 | ~6.3 Hz | 440 × 248 | 2.5 µm |
| 9-3, 9-4 | 50,000 | 6 | ~8.6 Hz | 440 × 248 | 2.5 µm |
| 9-6 | 57,000 | 4 | ~9.6 Hz | 600 × 372 | 1.667 µm |

That makes 144 fields and 5,908,896 pages per export.

**Metadata.** DANDI 000402 (version 0.230307.2132, CC BY 4.0) holds one NWB
file per scan, `sub-17797/sub-17797_ses-<session>-scan-<scan>_behavior+image+ophys.nwb`,
62 to 96 GB each, in `s3://dandiarchive` (us-east-2). Each file holds the same
movie again, gzip-compressed, beside the processed data. The store reads only
the metadata the TIFFs lack.

**Copies.** `functional_data/platinum_camovies/` holds 22 TIFFs,
`CaMovie_17797-<session>-<scan>-v1.tif`. Nineteen have the same ETag as the
v1 file of the same scan and are left out as copies. Scans 5-4, 5-5 and 8-4
are only there, with the v1 layout (320,000 pages, 14-tag directories after
the pixels) and no DANDI NWB file; phase 1 leaves them out (PLAN.md).

## Each TIFF

tifffile wrote every file as one shaped series, with
`ImageDescription = {"shape": [volumes × fields, rows, columns]}`:

- the first directory at byte 16;
- every page's pixels back to back, one uncompressed strip per page, from
  byte 406 (v2) or 512 (v1);
- every other directory after the pixels, packed at a constant stride: 236
  bytes (11 tags) in v2, 296 bytes (14 tags) in v1.

All 38 files have this layout. The trial took every page's offset from the
file's own directory (through tifffile) and found the pages contiguous and of
one size in every file. The directory block is 51 to 90 MB per file, 3.1 GB
in all.

The pages interleave the fields. Page `v × F + (n − 1)` is field `n` of volume
`v`, where `F` is the scan's field count. The documentation states this
order, and the trial confirmed it against the NWB copy (below).

## The two exports

v1 was exported on 2021-07-02 and v2 on 2023-01-13; the documentation's
changelog says v2 "corrected pixel over/under-flow error in export". The
pixel bytes are the same. Comparing 72 pages in scans 4-7, 9-3 and 9-6, every
v1 value equals the v2 value read as unsigned: `v1 − v2` is 0 everywhere
except where v2 is negative, and there it is exactly 65,536. What differs:

- `SampleFormat`: v1 states UINT, v2 states INT;
- v1 carries `XResolution` and `YResolution` of 1/1 and `ResolutionUnit` NONE
  in every directory, which v2 lacks; this accounts for v1's three extra tags
  and its larger directories;
- the offsets, because of the larger directories.

The pixels are signed. Motion correction leaves a few negative values,
from −167 to −1, in 0.1 to 0.3% of the pixels compared, and v1 reads them as
65,369 to 65,535. The documentation says the files are uint16; that is true of
v1 only.

The store keeps both, as two commits on `main`: tag `v1` references the v1
files as uint16, reproducing the export's error, and tag `v2` references the
v2 files as int16. The ingest compares the first, middle and last page of
every scan byte for byte before writing the second commit.

## Each NWB file

| Path | Holds |
|---|---|
| `acquisition/TwoPhotonSeries<n>` | field `n`'s movie, `(volumes, columns, rows)` int16, gzip, chunks `(2254, 14, 14)` |
| `acquisition/TwoPhotonSeries<n>.attrs["description"]` | "Calcium imaging data for field n at 6.3009 Hz and 8e-05 meters depth." |
| `acquisition/TwoPhotonSeries<n>/timestamps` | `(volumes,)` float64 seconds |
| `acquisition/TwoPhotonSeries<n>/imaging_plane` | a soft link to `general/optophysiology/ImagingPlane<n>` |
| `general/optophysiology/ImagingPlane<n>/origin_coords` | `[x, y, depth]` in meters |
| `general/optophysiology/ImagingPlane<n>/grid_spacing` | `[2.5e-06, 2.5e-06]` meters |
| `general/optophysiology/ImagingPlane<n>/imaging_rate` | NaN |

**Field n is TwoPhotonSeries n, transposed.** In scan 4-7 at volume 1000,
each `TwoPhotonSeries<k>` patch of 28 × 42 pixels equals the TIFF page
`1000 × 8 + (k − 1)` with rows and columns swapped, exactly, and no other page
of that volume. The NWB conversion transposed each frame from (height, width)
to (width, height) ([MICrONS-to-nwb](https://github.com/catalystneuro/MICrONS-to-nwb/blob/main/src/microns_to_nwb/tools/ophys/ophys.py)).

**`origin_coords` is the field's center.** The conversion fills it from the
DataJoint `Field` table's `field_x`, `field_y` and `field_z`, which the
processed-data documentation (v8) describes as the field center in motor
coordinates: x posterior to anterior, y lateral to medial, z from the pia
toward white matter. NWB defines `origin_coords` as the position of the first
pixel; the store follows what the values are.

**x runs along the TIFF's columns, y along its rows.** Coplanar fields 1 and
2 of scan 4-7 differ by 580 µm in `origin_coords[0]`: the 620 µm width of a
248-column field less the documented 40 µm overlap. The right edge of field 1
matches the left edge of field 2 (correlation 0.56 to 0.71 for the four
coplanar pairs in one frame) and not the reverse (0.19 to 0.33), so x
increases with the column index. The best overlap in that single frame was 15
to 31 columns, against the 16 that 40 µm implies; one frame is noisy, so the
size of the overlap is not settled.

**Every field carries the same timestamps.** They are the scan's volume
times: the conversion passes one `frame_times` array per scan, from the
DataJoint `ScanTimes` table, to every field. They are not the time at which
each field was scanned within its volume, which the NWB does not state.

**The rate is only in the description.** `imaging_rate` is NaN. The stated
rate agrees with the median volume interval to well under 1%; over a whole
scan the timestamps run 2.5 s (4-7) to 3.0 s (9-6) longer than the stated
rate implies, so the image's time scale is nominal and `volume_times` is
exact.

**Other inconsistencies, kept as stated.** `field_of_view` lists
`[0.00062, 0.0011]` (columns' extent first), while `dimension` lists
`[440, 248]` (rows first). Scan 9-6's two depths are 155 and 175 µm. Both are
stored in each field's `annotations.nwb` as the file states them.

## The store

The store is the Arraylake repo `earthmover-biology/MICrONS`. Its one virtual
chunk container is the BossDB scan prefix,
`s3://bossdb-open-data/iarpa_microns/minnie/functional_data/two_photon_functional_scans/`, read anonymously
in us-east-1; every chunk reference falls under it.

| Node | From |
|---|---|
| `/` | title, description, license, citations, sources, `layout`; `tiff_export` names the commit's export |
| `two-photon` | a description and one entry per scan: fields, volumes, rate |
| `two-photon/<session>-<scan>` | `ome.scene`: coordinate system `motor` (t, z, y, x) and one translation per field; `scan`: animal, session, scan, counts, rate, depths, the NWB and TIFF URLs |
| `two-photon/<session>-<scan>/volume_times` | the NWB timestamps, `(volumes,)` float64 seconds, written natively with Blosc zstd and byte shuffle |
| `two-photon/<session>-<scan>/field-<n>` | an image: `ome.multiscales` with one level and scale `(1 / rate, 1, pixel, pixel)`; `annotations`: what the NWB states about the field, the page rule and the export; `tiff`: what the TIFF states about its series |
| `two-photon/<session>-<scan>/field-<n>/0` | `(volumes, 1, rows, columns)`, chunks `(1, 1, rows, columns)`; chunk `(v, 0, 0, 0)` references page `v × F + n − 1` |

### How the hierarchy declares its images

The OME-Zarr metadata starts at the scans. OME-Zarr 0.6 defines a scene as
"the group above the images", "a collection of images that share a spatial
relationship with each other", which "MUST implement the scene
specification" ([Scene](https://ngff.openmicroscopy.org/specifications/0.6/index.html#scene-format), storage format). The
`scene` object's `coordinateTransformations` name each image by the `path`
of a transformation's input, and its `coordinateSystems` may define the
shared system they map into (["scene" metadata](https://ngff.openmicroscopy.org/specifications/0.6/index.html#scene-md)).
Each scan group is such a scene: its coordinate system is `motor`, and it
holds one translation per field, from `{"name": "intrinsic", "path": "field-<n>"}`
to `{"name": "motor"}`. The fields are therefore declared once, by the scene
that places them.

Nothing in the spec covers a group of scenes, so `two-photon` and the root
carry no `ome` key, only attributes that describe what is below them. The
spec's other way to list images, `bioformats2raw.layout`, is marked
transitional and exists "to specify filesets that already exist in the
wild" ([bioformats2raw.layout](https://ngff.openmicroscopy.org/specifications/0.6/index.html#bf2raw));
here it would repeat the 144 field paths in an `OME` group beside the
hierarchy that already holds them, so the store does not use it.
`minus80.ome.hierarchy` finds a scene's images through its paths, so the
shared OME-Zarr gates reach every field. Run 1 wrote the store with the collection;
`scenes.py` (run 2) removes it in one metadata-only commit, leaving every
array and chunk reference as it was.

The scene places field `n` at `t` = the scan's first volume time, `z` = its
depth, and `(y, x)` = its center less half the field, so that the
translation is the center of the first pixel, as OME-Zarr reads it. Scan 4-7
field 1 sits at (10.15 s, 80 µm, −783.75 µm, −793.75 µm).

## Judgement calls

- **The z axis.** Each field is one optical section. A size-1 z axis lets the
  scene place it at its depth; its scale of 1 µm is a placeholder, named as
  one in the attributes. PLAN.md weighs this against a plain (t, y, x).
- **Volume times per scan.** The NWB gives every field the same timestamps,
  so they are stored once per scan, not once per field.
- **Centers, not corners.** `origin_coords` is used as the field center, from
  the conversion code and the DataJoint schema, against the NWB definition of
  the field.
- **Both exports.** v1 is kept, with its wrong dtype, as the first commit, so
  a reader of an analysis made on v1 can check out what it read.

## Phase 2: processed data and EM matches

Proposed in [PLAN-phase2.md](PLAN-phase2.md); nothing below is in the
published store yet. The figures are `processed-source.svg`,
`processed-mapping.svg` and `processed-store.svg`.

### The NWB file, whole

Each scan's DANDI file is written at `two-photon/<session>-<scan>/nwb` as
shadowzarr maps it, in the unified NWB Zarr v3 convention: NWB's paths,
types (`neurodata_type`), links (`_LINKS`) and references stay as the file
has them, and `dimension_names` are added. What a reader finds there, per
field `n`:

| Path under `nwb/` | Holds |
|---|---|
| `processing/ophys/ImageSegmentation/PlaneSegmentation<n>/image_mask` | `(rois, columns, rows)` float32 weights, gzip, chunks `(87, 16, 28)` |
| `.../PlaneSegmentation<n>/id` | `1 … n`, the ROI's id within the field |
| `.../PlaneSegmentation<n>/mask_type` | `soma` or `artifact` |
| `.../PlaneSegmentation<n>/pt_*`, `cave_ids`, `cave_ids_index` | on fields 2, 4, 6 and 8 only: the EM point (4 × 4 × 40 nm voxels), root and supervoxel ID that CAVE v343 gave, float64 |
| `processing/ophys/Fluorescence/RoiResponseSeries<n>/data` | `(time, roi)` float32, gzip, chunks `(625, 44)` |
| `.../RoiResponseSeries<n>/timestamps` | equal to the movie's timestamps (field 4 of scan 4-7), so to phase 1's `volume_times` |
| `processing/ophys/SegmentationImages<n>/average`, `correlation` | `(columns, rows)` float64, contiguous |
| `acquisition/TwoPhotonSeries<n>/data` | field `n`'s movie, the same values as `field-<n>`, transposed |

and per scan:

| Path under `nwb/` | Holds |
|---|---|
| `acquisition/EyeTracking/eye_position/data` | `(104227, 2)` float64, pixels |
| `acquisition/PupilTracking/pupil_{major,minor}_radius/data` | `(104227,)` float64; only the minor radius has its own `timestamps` |
| `acquisition/treadmill_velocity/{data,timestamps}` | `(480032,)` float64, `conversion` 0.01 to m/s |
| `acquisition/Video: stimulus_17797_<s>_<n>_v4/data` | `(frames, 144, 256, 3)` uint8, one frame per gzip chunk |
| `intervals/{Clip,Monet2,Trippy}` | the trial tables, one dataset per column |

The NWB file has no deconvolved activity: `processing/ophys` holds only
`Fluorescence`. A RoiResponseSeries' `data` and its `rois` region share the
`roi` dimension name, and `data` and `timestamps` share `time`, so
`xr.open_zarr` opens the series as one dataset.

### The masks

`rois-<n>` holds field `n`'s `image_mask` transposed to the TIFF's
`(rows, columns)`, as `(roi, 1, rows, columns)` float32, one chunk per ROI
inside one shard, Blosc zstd with byte shuffle. ROI `r` of the image is ROI
id `r + 1` of the field's PlaneSegmentation. In scan 4-7:

- the masks are 3.66 GB uncompressed, 9.2 MB as DANDI stores them, and
  4.8 MB written this way;
- every weight is positive, from 0.012 to 3.38 in field 4, over a median of
  141 pixels per ROI;
- of the 653,578 pixels some ROI covers, 374,274 are covered by two or more,
  which is why the store does not reduce them to a label image.

The image's translation is its field's place in the scene's motor
coordinates; its `annotations` name the field, the NWB table, the traces and
the first unit.

### The unit rule

CAVE names a two-photon cell by `(session, scan_idx, unit_id)`; the NWB
names it by field and ROI id. No file states how one maps to the other. Its
source is DataJoint's `ScanUnit` table, which ships only in the MySQL dump.
The mapping is:

```
unit_id = (ROIs in fields 1 … n − 1 of the scan) + ROI id
```

The evidence:

- In all 19 NWB files, each field's `id` runs 1 to the field's ROI count.
- Every row of every match table with a `field` column (26 table versions,
  13,925 to 84,233 rows each) has a `unit_id` that the rule places in that
  field.
- In scan 4-7, all 687 rows of v343's `functional_coreg` give the same EM
  point as the NWB columns of the ROI the rule names.

The store writes `field` and `roi` beside `unit_id` in each scan's `units`,
and says in its attributes that the rule is derived. The run checks it again
on every row of every table it writes, and raises on one that disagrees.

### The CAVE match tables

CAVE publishes static dumps of its tables for some versions at
`gs://mat_dbs/public/minnie65_phase3_v1/<version>/<table>_merged.csv.gz`, each
a whole-file gzip CSV without a header, with `<table>_merged_header.csv`
naming the columns and their types. Twelve versions have a directory; eight
of them (v343, v661, v795, v943, v1078, v1181, v1412, v1507) hold match
tables. v1300's dump has three tables, none of them matches. The objects are
dated 2024-09 to 2026-06, later than most of the versions they hold, so the
store records each object's generation and date.

- **Rows stay, IDs change.** Every table keeps the same rows, with the same
  `id` and supervoxel ID, in every version that lists it. Only
  `pt_root_id` changes, in 1.4 to 12.3% of rows from one version to the next.
- **One row per unit,** except v343's `functional_coreg`, whose 9,518 rows
  hold 9,434 distinct matches: 84 rows repeat another row in everything but
  `id`. The store keeps one of each.
- **The NWB's EM columns are v343's `functional_coreg`:** 9,434 ROIs carry an
  EM point, the same as the table's distinct matches.
- **Even fields.** The matches lie on fields 2, 4, 6 and 8;
  `coregistration_manual_v4` has no row on an odd field, and the combined
  automatic table has 47, all on field 1.
- **Scans 5-4, 5-5 and 8-4.** The automatic tables from v943 on cover 22
  scans: the 19 on DANDI and the three published only in
  `platinum_camovies/`. Those 11,210 to 12,213 rows per table cannot join an
  NWB file and stay out of the store.

Each table becomes `units/em/<table>/` in its scan, with one array per
column on the `units` axis, and each version is a commit: the ingest writes
the arrays a version changes in place, adds the tables it introduces, and
deletes the tables it no longer lists. Root and supervoxel IDs are uint64
and exact, unlike the NWB's float64 copies, which lose the last two or three
digits of an 18-digit ID.

### Judgement calls

- **Duplicates dropped.** v343's 84 repeated `functional_coreg` rows differ
  only in `id`, so one of each is kept; the commit's metadata records the
  dump's row count and how many repeats were dropped.
- **Matches in the store, not only in Iceberg.** The columns are
  coordinates on the `units` axis, and a tag pins them with the traces; the
  full tables, rows for scans without an NWB file included, go to Iceberg
  later.
- **Masks as an image.** The weights are float32 and overlap, so they are an
  image with a `roi` axis, not an OME-Zarr label image.
- **Masks outside the scene.** A 0.6 scene's images share its axes, (t, z, y,
  x), and the ROI axis is not time, so each mask image carries its field's
  translation itself.
- **The NWB file inside the scan group.** `two-photon/<scan>/nwb`, not a
  sibling tree, so one group holds everything about a scan.
