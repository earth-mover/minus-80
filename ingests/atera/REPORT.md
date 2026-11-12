# 10x Atera WTA kidney: ingest report

## What was built

The Arraylake repo `earthmover-biology/atera-kidney` holds the kidney sample, `Atera_v1_WTA_Kidney_non_diseased_FFPE`.
The branch is `main`. Its snapshot is `KB0DWEXDHY8GSRE29GQG`.
The store is a native SpatialData store. The ingest copied the bytes. It did not reference 10x's bucket.
The tables are in the namespace `atera-kidney-tables` of the catalog `arraylake://earthmover-biology`.
The run took place on 2026-11-11 and 2026-11-12. The sample is one of four. "Not done" lists the other three.

### The tree of `main`

| Element | Shape and type | Levels | Source |
|---|---|---|---|
| `images/morphology` | `(4, y, x)` `uint16`, level 0 is `(4, 33139, 56644)`, chunks `(1, 1024, 1024)` | 8 | four OME-TIFF pyramids, decoded once |
| `images/morphology_3d` | `(1, 13, y, x)` `uint16`, 13 planes, chunks `(1, 1, 1024, 1024)`, shards `(1, 13, 1024, 1024)` | 8 | one OME-TIFF series, decoded once |
| `images/he_image` | `(3, y, x)` `uint8`, level 0 is `(3, 42169, 23628)` | 7 | the H&E OME-TIFF, decoded once |
| `images/qc_transcript_density`, `qc_mean_q_score`, `qc_negative_control_codewords`, `qc_negative_control_probes`, `qc_genomic_controls` | `(1, 353, 603)` `uint16`, 20 µm bins | 1 | the analysis summary report |
| `images/qc_mean_q_score_80um` | `(1, 89, 151)` `uint16`, 80 µm bins | 1 | the analysis summary report |
| `labels/nucleus_labels`, `labels/cell_labels` | `(33139, 56644)` `uint32`, chunks `(518, 1771)` | 1 | `cells.zarr.zip`, chunks copied unchanged |
| `shapes/nucleus_boundaries`, `shapes/cell_boundaries` | Iceberg table and a `uint8` copy of its Parquet file | | `cells.zarr.zip` |
| `points/transcripts` | Iceberg table in 30 partitions and 30 `uint8` copies | | `transcripts.zarr.zip` |
| `tables/table` | AnnData, `X` CSR `(399120, 24674)`, 599,212,411 stored values | | `cell_feature_matrix.zarr.zip`, chunks copied unchanged |

The root attributes hold the title, citation, licence, source objects, sample and run records, and the list of what the store lacks.
The root attribute `iceberg_tables` pins three tables that annotate no element.
The root attribute `reading` says which reader opens which element.
`tables/table` annotates `cell_boundaries`. It has one row per cell.
Its `instance` column equals the cell mask label. So the labels and the nucleus shapes join to the table by `instance`.
The 3D stack has 13 planes at 3 µm. The H&E has its own pixel grid and an affine into `global`.
The coordinate system `global` is in micrometres.

### The Iceberg tables

Each pin is the one that `main` holds.

| Table | UUID | Snapshot | Rows | Data files |
|---|---|---|---|---|
| `transcripts` | `9c8ad66e-9714-441a-a64c-47717a6df18e` | `7895471496307373462` | 1,186,813,834 | 30, partitioned by `grid_tile` |
| `nucleus_boundaries` | `92d035bd-24ee-43a6-aad6-e459230a552c` | `8489201415584545528` | 394,076 | 1 |
| `cell_boundaries` | `7e2b39f1-afde-4142-9ddb-9ff48db2fe99` | `5255001402189237136` | 399,120 | 1 |
| `codewords` | `7a62569c-55e6-4838-9150-f89030eae6af` | `6441432961357742944` | 53,385 | 1 |
| `diffexp_azimuth` | `16e63f48-9cb2-422f-ab8c-8f7d813ca006` | `4329143566662092150` | 18,045 | 1 |
| `diffexp_graphclust` | `8381aeea-9fea-4d8c-a0f7-b461c776e10d` | `4409843005460204845` | 18,045 | 1 |

The ingest replaced the rows of `transcripts` and `nucleus_boundaries` in place, with the flag `--replace-tables transcripts,nucleus_boundaries`.
Both tables kept their names, namespace and UUIDs.
On `nucleus_boundaries`, one schema commit added the columns `part_count` and `methods` and deleted the column `method`.
One overwrite then replaced the rows.
On `transcripts`, the held tile `0,0` had instances 0 to 2,633,911. The ingest wrote it again at instances 1 to 2,633,912.
`transcripts` has 32 snapshots. `nucleus_boundaries` has 3. The ingest deleted no snapshot and no data file.

The `fixture` branch is unchanged. A check from a laptop, with no read of the source, found these facts:

- It is at snapshot `BF52XEQ53GH7JRBE4F30`.
- Its gates `copies`, `attrs`, `field_ids` and `table_index` pass.
- Its pinned snapshots load. `transcripts` `5371144533979842643` has 1 file and 2,633,912 rows.
- `nucleus_boundaries` `3733681804133973613` has 1 file and 398,197 rows, with the `method` column.
- The data files of both snapshots exist.

### Where it ran

| Item | Value |
|---|---|
| Machine | one Coiled VM, `m7g.xlarge` (Graviton, `aarch64`, 4 vCPU, 16 GiB) |
| Place | AWS us-west-2, Coiled workspace `earthmover-devs` |
| Disk | 100 GiB requested, a 96 GiB volume, 87 GiB free at the start |
| Launch | 2026-11-11 22:06 UTC, `launch.py --ingest`, code at commit `c658dfa` |
| Concurrency | `--concurrency 32` |
| Limit | 480 minutes on the server |
| Credential | the Arraylake machine token, passed as a Coiled secret |
| Result | last script line `=== all steps passed`, launcher exit code 0, cluster stopped |

The VM downloaded the three source objects once, one stream each: 48,518,883,775 bytes in 991 s.
It checked each size and recomputed each multipart ETag. Then it read local files.

Three operations were checked on Arraylake before the run, on tables that were made for the check and dropped.
They are a staged create of a partitioned table, a schema change with one delete and two adds in one commit, and an overwrite of one partition.
In each case the earlier snapshot read its rows after a fresh load.

### How to open it

SpatialData 0.8.0, as released, cannot open an Icechunk store.
The reader that opened every element of the fixture is SpatialData from pull request 1107 with four patches.
It also needs the virtual_omics shim `parquet_in_store()` and the Zarr config `array.rectilinear_chunks`.
The shim reads the `uint8` copies of points and shapes.
zax-spatialdata reads the Iceberg tables through the Arraylake catalog.
It checks each `iceberg_table` attribute against the table UUID and snapshot.
The root attribute `reading` says which reader opens which element.
These reads ran on the `fixture` branch. Nobody ran them on `main`.
To read `main`, use the branch name `main` in place of `fixture`. That change is not tested.
Read each table at the snapshot that its element pins. The table's current snapshot can differ.

## Verification

`verify.py --sample kidney --mode full` read snapshot `KB0DWEXDHY8GSRE29GQG` and the local files, on the VM that wrote the store.
All 16 gates passed in 2,246 s. The run read 1,619.2 MiB of the bundle in 7,761 requests.

| Gate | Result | Counts |
|---|---|---|
| `complete` | PASS | 16 elements of the full tree present, 4 listed as missing |
| `images` | PASS | 143 pages, at most four tiles each: 32 pages of `morphology`, 104 of `morphology_3d`, 7 of `he_image` |
| `qc` | PASS | each of the six rasters equals the report's |
| `labels` | PASS | 2,048 of 2,048 chunks of `masks/0` and of `masks/1` |
| `matrix` | PASS | CSR `(399120, 24674)`, 599,212,411 stored values; 1,617 chunks of 419 arrays |
| `tables` | PASS | `transcripts` 30 tiles, 1,186,813,834 rows, as the source; `cell_boundaries` 399,120 rows; `nucleus_boundaries` 394,076 rows |
| `masks` | PASS | 5,044 of 399,120 `cell_summary` rows have no nucleus; background at 0 of 50 nucleus centroids in each mask |
| `instances` | PASS | 50 rows, 0 mismatches |
| `polygons` | PASS | `cell_boundaries`: 399,120 cells, 399,120 parts, 397 parts not valid; `nucleus_boundaries`: 394,076 cells, 398,197 parts, 1,180 parts not valid |
| `table_region` | PASS | the table's instances equal those of `cell_boundaries` |
| `copies` | PASS | `transcripts` 30 arrays and 30 registered files, each shapes table 1 and 1; 132 blocks of 8 MiB |
| `attrs` | PASS | 3 `iceberg_table` attributes |
| `extra_tables` | PASS | `codewords` 53,385 rows, `diffexp_azimuth` 18,045, `diffexp_graphclust` 18,045, each as the source; field ids: `codewords` 12 fields, `diffexp_azimuth` 146, `diffexp_graphclust` 86 |
| `field_ids` | PASS | `transcripts` 30 files, 17 fields; each shapes table 1 file, 11 fields |
| `table_index` | PASS | `obs` 399,120 rows, 18 columns, index `barcode`; `var` 24,674 rows, 385 columns, index `feature_id` |
| `source` | PASS | five source objects: size and ETag from a HEAD request equal the record |

The `masks` gate gives these median relative errors.
The median label area is 26.82 µm² for nuclei and 57.21 µm² for cells.

| Pair | Error |
|---|---|
| `nucleus_labels` against `nucleus_area` | 0.000 |
| `cell_labels` against `cell_area` | 0.000 |
| `nucleus_labels` against `cell_area` | 0.502 |
| `cell_labels` against `nucleus_area` | 1.009 |

### What the gates compare

- Every copied chunk of the matrix and of both masks equals its source member, by hash.
- Decoded image tiles equal tifffile's decode. The sample is four tiles of every level of every plane.
- Each transcripts tile has the row count and the `instance` range of the source tile. The ranges leave no gap across tiles.
- Each row group of a tile starts after the last one. The Parquet footers give this.
- Each polygon table has one row per cell of its set. `part_count` equals the source rows of every cell.
- For each Parquet data file, the first block, the last block and three blocks between equal the `uint8` copy, by hash.
- Each source object's size and ETag equal the record in the root attribute `source`.

### What the gates do not prove

- The `images` gate samples tiles. It does not find a wrong tile outside the sample.
- The gate and the ingest both decode with tifffile, which calls `imagecodecs`. The gate does not compare two decoders.
- The `transcripts` check proves contiguity at row-group granularity.
  It does not find a repeat inside one row group that has the right minimum, maximum and count.
- The `copies` gate hashes at most five blocks of each data file. It does not find a change in another block.
- Booth and the Arraylake web app are not checked.
- The overview scan is not in the store. No gate covers it.
- No gate checks an element's transform.
- Two checks use the ingest's own decoders as the reference: the QC rasters' decode and `zarr_copy.read_array`.
  The `tables` and `polygons` gates decode `polygon_sets/*/cell_index` with `read_array`.
  The `masks`, `instances` and `polygons` gates get `cell_summary` and `cell_id` from `read_array`.
  The ingest decodes `cell_index` and `cell_id` with the same function.

## Changes from the plan

| Plan | Run |
|---|---|
| The fixture's `nucleus_boundaries` had several rows for one cell. | One row per cell. The columns `part_count` and `methods` replace `method`. A cell with several parts is a `MultiPolygon`. |
| The fixture's table annotated `cell_labels`. | `tables/table` annotates `cell_boundaries`. The gate `table_region` checks it. |
| The H&E: keep its pixels. | The ingest decoded every H&E level. The kidney's Deflate tiles have no codec in the Zarr core. One rule serves all four samples. |
| The full ingest writes `transcripts` and `nucleus_boundaries`, which the fixture pins. | The ingest replaced their rows in place with `--replace-tables`. The fixture's pins stay valid. |
| The ingest reads the bundle from 10x's bucket by range request. | The VM downloaded the three objects to its disk once. The ingest read local files. |
| The first measurement ran on an `m7i.xlarge`. | The full run used an `m7g.xlarge`, 4 vCPU and 16 GiB, with `--concurrency 32`. |
| The QC rasters are `uint16` or `float32`. | All six rasters of the kidney are `uint16`, the quality maps included. |
| The plan's first count for the kidney was 28 tiles. | The kidney has 30 tiles. Tiles `-1,0` and `5,-1` hold 1 and 20 rows. |
| One chunk length for the columns of a transcript tile. | The columns have different chunk rows. The ingest slices a tile in whole chunks of its widest column. |
| The 3D stack: one plane at a time. | The ingest decodes one tile from every plane and writes its shard once. |

The ingest wrote each image level as one commit.
It marks each image complete with the attribute `minus80:complete`, and it resumes on that attribute.
The plan estimated the time of the measured kinds from rates. The run gives these times:

| Kind | Estimate | Run |
|---|---|---|
| 2D morphology | 759 s | 625.0 s |
| 3D stack | 5,651 s | 3,257.4 s |
| H&E | 158 s | 68.5 s |
| Two masks | 123 s | 69.9 s |
| Transcripts | 2,163 s, Parquet files only | 1,749.3 s, with upload and commit |
| Sum | 8,854 s | 5,770.1 s |

The estimate read the bundle from 10x's bucket. The run read local files.

## Judgement calls made

The numbered calls of `PLAN.md` follow. The owner has not yet checked calls 5, 7, 13 and 14, and the call on levels above 0.

| # | Call | As built |
|---|---|---|
| 1 | Mask order | `masks/0` is the nucleus mask, `masks/1` the cell mask. The `masks` gate gives errors of 0.000 against the right area, and 0.502 and 1.009 against the other. |
| 2 | Instance key | The mask label. The `instances` gate has 0 mismatches in 50 rows. `tables/table` annotates `cell_boundaries` only. |
| 3 | `quality_score` type | Not changed in stage two. |
| 4 | Global frame | Not changed in stage two. |
| 5 | The H&E | Open. The ingest keeps its pixels, decodes them and places them with the affine. |
| 6 | The overview scan | Not written. "Not done" says why. |
| 7 | The 3D stack | Open. It is in the store, in shards of 13 planes. |
| 8 | Transcript partitioning | One partition per source tile, 30 partitions. |
| 9 | Differential expression | Two tables, `diffexp_azimuth` and `diffexp_graphclust`. |
| 10 | Namespace | `atera-kidney-tables`. |
| 11 | `AGENTS.md` | Not changed in stage two. |
| 12 | Virtual references for the `uint8` copies | Kept. The `copies` gate passes. |
| 13 | Tables that annotate no element | Open. The root attribute `iceberg_tables` pins `codewords`, `diffexp_azimuth` and `diffexp_graphclust`. |
| 14 | OME version | Open. The raster elements declare `0.5-dev-spatialdata`. |

Open calls, for the owner to check:

- **Call 5.** The H&E is decoded, not copied. One rule serves all four samples.
- **Call 7.** The 3D stack took 3,257.4 s of the ingest.
- **Call 13.** SpatialData has no element type for a table that annotates no element.
- **Call 14.** A strict validator, `ome-zarr-models` 1.8.1, rejects the string `0.5-dev-spatialdata`.
  With `0.5`, it validates the attributes. SpatialData writes the string `0.5-dev-spatialdata`.
- **No translation on levels above 0.** Each level has a scale that gives its pixel size.
  No translation aligns the pixel centres of levels above 0 with level 0.

## Problems found in the source data

| Problem | Count |
|---|---|
| The nucleus polygon set has several rows for one cell. | 398,197 rows for 394,076 cells. 3,926 cells have more than one part, at most 4. 1,076 cells have parts of mixed methods. |
| Cells have no nucleus. | 5,044 of 399,120. Their nucleus centroid and area columns in `cell_summary` are NaN. |
| Nucleus polygons are not valid. | 1,180 parts of 398,197. |
| Cell polygons are not valid. | 397 parts of 399,120. |
| The columns of a transcript tile have different chunk rows. | The array `location` has 87,382 chunk rows. The least common multiple in tile `3,1` is 45,813,334,016. |
| The density map counts fewer transcripts than the store holds. | The map sums to 1,083,945,548. The report states 1,083,799,964 high quality transcripts. The store holds 1,186,813,834. |
| The H&E pixel size differs from the alignment's scale. | 0.27377 µm in the OME-XML and 0.27387 µm from the matrix: 0.04 percent. |
| The report has two `ws-data` texts. | The first is inside an HTML comment. The data bag follows the last one. |

The ingest keeps invalid polygons as published. It does not claim what the density map counts.
The store keeps the alignment matrix as published and places the H&E by it.

## Upstream issues hit

[docs/upstream.md](../../docs/upstream.md) has the details of each row.

| Row | Section | Effect on this ingest |
|---|---|---|
| pyiceberg maps `uint32` to a signed `int` | pyiceberg | The ingest declares each `uint32` column as `int64`. |
| pyiceberg `add_files` leaves files without field ids | pyiceberg | `write_parquet` writes the field ids. The `field_ids` gate passes on the three element tables. The `extra_tables` gate checks the field ids of the three other tables. |
| SpatialData cannot open a store with no filesystem path | SpatialData | The store keeps the `uint8` copies. A read needs [spatialdata#1107](https://github.com/scverse/spatialdata/pull/1107). |
| A regular chunk grid cannot hold a short last chunk | zarr | `declare_copy` writes a rectilinear grid. A reader needs `array.rectilinear_chunks`. |
| `ZipStore` cannot use an open `zipfile.ZipFile` | zarr | `read_array` reads a Zarr v2 array from the open zip. |

## Timings

All times come from the run's log. Each level of an image is one commit.

| Step | Seconds |
|---|---|
| environment | 13 |
| preflight | 14 |
| download | 991 |
| check | 377 |
| ingest | 6,829 |
| verify | 2,246 |

The disk held 9.5 GiB before the download and 55 GiB after it.

| Element | Read | Seconds |
|---|---|---|
| `tables/table` | 1,196.2 MiB | 905.3 |
| `labels/nucleus_labels` | 120.1 MiB | 32.6 |
| `labels/cell_labels` | 151.7 MiB | 37.3 |
| `shapes/nucleus_boundaries` | 55.6 MiB | 16.0 |
| `shapes/cell_boundaries` | 56.1 MiB | 14.8 |
| `points/transcripts` | 13,467.8 MiB | 1,749.3 |
| `iceberg_tables` | 0.0 MiB | 30.5 |
| six `images/qc_*` elements | 0.0 MiB each | 2.8 to 3.4 each, 17.9 in all |
| `images/morphology`, levels 0 to 7 | 4,758.3 MiB at level 0 | 625.0 in all, 429.9 at level 0 |
| `images/morphology_3d`, levels 0 to 7 | 15,643.8 MiB at level 0 | 3,257.4 in all, 2,508.1 at level 0 |
| `images/he_image`, levels 0 to 6 | 1,992.2 MiB at level 0 | 68.5 in all, 40.7 at level 0 |
| root attributes | 0.1 MiB | 1.6 |

The count matrix is the slowest small step: 1,196.2 MiB in 905.3 s. Its copy runs with one writer.
The 3D stack is the longest step. Its decode runs in one thread.

## Not done

- **Three samples.** The brain, colorectal and breast samples are not ingested. Each needs its repo and its access policy.
- **The overview scan.** `images/overview_scan` is not written. The ingest does not place the scan. `PLAN.md`, call 6, gives the test for it.
- **Booth.** Nobody checked whether Booth serves an element as tiles.
- **Reads on `main`.** The SpatialData and zax reads ran on the `fixture` branch only.
- **What `missing` lists.** The root attribute `missing` lists the overview scan and three bundle members.
  They are `csc_cell_feature_matrix.zarr.zip`, `binned_transcripts.zarr.zip`, and the HTML reports' thumbnails, plots and interface.
