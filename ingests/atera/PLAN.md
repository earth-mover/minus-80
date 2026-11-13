# 10x Atera WTA: ingest plan

## Summary

This ingest copies four 10x Atera whole-transcriptome samples into Arraylake as
native Icechunk repos, one repo per sample, in the `earthmover-biology` org. It
is the first native ingest in minus-80. Every other ingest references the
archive's bytes in place. This one copies them. 10x publishes these bundles as
downloads in a public bucket. A store of references would send every reader's
range reads to that bucket.

Each repo is a SpatialData store. Images and labels are OME-Zarr 0.5 pyramids.
The counts are an AnnData table. Transcripts and cell polygons go to Arraylake
Iceberg tables beside the repo. The same Parquet files also appear inside the
repo as `uint8` arrays at the SpatialData paths. Python SpatialData reads
those through a shim, and zax reads the tables directly.

**Status.** All four samples are ingested. Every gate passed, and `main` of each repo is at the verified snapshot:

| Repo | `main` |
|---|---|
| `atera-kidney` | `DCHK9523X4W5TSC4V5X0` |
| `atera-brain` | `KQQGSNM0K4Z4HQT3R050` |
| `atera-colorectal` | `SG4W57AMEJYGKZT9K250` |
| `atera-breast` | `PGYV9R98EKVJRE85J2Z0` |

`REPORT.md` gives the results of each run. "Steps, scope and risk", "Open items", lists the work that remains.

**Asked of the reviewer:** approve the layout and the steps, and decide the
judgement calls below. Each has a recommended default.

| # | Decision | Recommended |
|---|---|---|
| 1 | Which mask is the cell mask and which the nucleus mask | Decided: `masks/0` nucleus, `masks/1` cell, as `polygon_set_names` orders them. The fixture confirmed it by area. |
| 2 | The table's instance key and region | Decided: the integer label in the masks, which is the row position plus one. On `main`, the table annotates `cell_boundaries` only. `obs` keeps `barcode` and `cell_id`. |
| 3 | Transcript `quality_score` is `float16` | Write `float32` in the Iceberg tables. Record the widening. |
| 4 | The global coordinate system | Micrometres, as `spatialdata_io.xenium` does. Each image carries `Scale(pixel_size)`. |
| 5 | The H&E image | Keep its pixels. Place it with the affine from the alignment CSV. Do not resample. |
| 6 | The overview camera scan | Place it by a least-squares fit of its FOV boxes to the morphology FOV boxes. Include it only if the fit's residual is under one morphology pixel. |
| 7 | The 3D DAPI stack | Include it as `(c, z, y, x)` with one channel, in shards of one xy tile across all planes. |
| 8 | Transcript table partitioning | One partition per source grid tile, plus a `bbox` struct per row for file pruning. |
| 9 | Differential-expression CSVs | Two Iceberg tables per sample. `var` keeps the per-cluster Leiden columns the source already holds. |
| 10 | Namespace names | One namespace per repo, `<repo>-tables`, as `cellpainting-gallery-tables` does. |
| 11 | A docs change | Add a "native ingests" section to `AGENTS.md` that states the exception to the zero-copy rules and why. |

Nothing is written to a shared resource before approval. "Steps, scope and
risk" lists each approval.

"Judgement calls" gives the decisions of the owner.

## The archive

10x Genomics publishes each Atera sample as one output bundle, `outs.zip`,
plus three objects beside it. Those are a post-run H&E OME-TIFF, a 3 by 3
alignment matrix as CSV, and `experiment.spatial`, a JSON record of the run.
The objects sit in `s3://10x.files/samples/atera/<version>/<sample>/`. The
bucket answers reads by key, also over plain HTTP, and refuses listings. The
dataset pages are at
[10xgenomics.com/datasets](https://www.10xgenomics.com/datasets?search=Atera&sort=publishedAt+DESC&tag%5Bchemistries%5D=v1).
They block automated reads, so the facts below come from the objects.

| Sample | Dataset page title | Analysis | Cells | Transcripts | Bundle | H&E | Published |
|---|---|---|---|---|---|---|---|
| `Atera_v1_WTA_Kidney_non_diseased_FFPE` | Atera In Situ Gene Expression, FFPE Human Kidney | 1.0.4 | 399,120 | 1,186,813,834 | 45.7 GB | 2.8 GB | 2026-10-15 |
| `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF` | Atera In Situ Gene Expression, Fresh Frozen Human Brain | 1.1.1 | 91,431 | 1,640,508,930 | 48.9 GB | 6.5 GB | 2026-11-03 |
| `Atera_v1_WTA_S4B_Colorectal_Cancer_FF` | Atera In Situ Gene Expression, Fresh Frozen Human Colorectal Cancer | 1.1.1 | 446,777 | 3,855,378,414 | 75.3 GB | 5.1 GB | 2026-11-05 |
| `Atera_v1_WTA_S4C_Breast_Cancer_FF` | Atera In Situ Gene Expression, Fresh Frozen Human Breast Cancer | 1.1.1 | 485,985 | 4,565,138,232 | 89.3 GB | 7.3 GB | 2026-11-05 |

The page URLs, the biomaterials, the protocol document numbers and each
object's size and ETag are in `LAYOUTS.md`, "Attribution". The published
dates are the pages' dates.
The cells and the transcripts come from each sample's record in `samples/`.
The cells are `cells.count`. The transcripts are `transcripts.number_rnas`.
In each record, the rows of the transcript tiles add up to `number_rnas`.
The bundle and H&E sizes come from the objects' sizes in `LAYOUTS.md`.

All four share the human whole-transcriptome panel `hWTA_v1` of 18,049 targets,
a pixel size of 0.21249222 µm, a z step of 3 µm, and the segmentation stain
set `atera_cell_segmentation_stains_v1`. The three fresh-frozen samples are
three regions of one slide, S4, from one run. The brain sample assigns 45
percent of its transcripts to cells; the others assign 84 to 88 percent.
`LAYOUTS.md`, "The four bundles", gives each difference between the samples.

**Licence and attribution.** The datasets are licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), which permits a
re-hosted copy with attribution. 10x's citation page gives one format for a
dataset, by example:

> PBMCs from C57BL/6 mice (v1, 150x150), Single Cell Immune Profiling Dataset
> by Cell Ranger v3.1.0, 10x Genomics, (2019, July 24).

So a dataset citation gives the dataset's title, the product, the software and
its full version, 10x Genomics, and the publication date. Software is cited as
"10x Genomics Atera Onboard Analysis v1.0.0", with the version stated. The
page lists no publication for Atera, so there is no DOI to cite. No trademark
symbol is needed in a citation.

The root attributes follow the keys the `cellpainting-gallery` repo uses, so a
reader finds them in the same place in every repo of the org:

| Key | Value |
|---|---|
| `title` | the dataset's title as the 10x dataset page gives it |
| `description` | what the store holds and how it is laid out |
| `source` | the five objects, the analysis summary included: URL, size, ETag and last-modified date of each |
| `documentation` | three URLs: the dataset page, the Atera Onboard Analysis output documentation, and the software documentation |
| `license` | `"Creative Commons Attribution 4.0 International (CC BY 4.0)"` and its URL |
| `citation` | `text`: the rendered citation in 10x's format, as `LAYOUTS.md` gives it; `dataset_title`; `product`: `"Atera In Situ"`; `software`: `"10x Genomics Atera Onboard Analysis v1.0.4"`, the version as the page states it; `software_version_full`: `analysis_sw_version`, such as `atera-1.0.4.1`; `instrument_software`: `instrument_sw_version`; `publisher`: `"10x Genomics"`; `published`: the dataset page's date; `url`: the dataset page; `doi`: `null`, with a note that 10x lists none |
| `reading` | which readers open which elements, and what the shim is for |
| `sample`, `run`, `panel`, `metrics` | `experiment.spatial` and the metrics member, typed. The kidney's member is `metrics_summary_RnD_S2_S2B_kidney_ND.csv`. The ingest finds the one member whose name starts with `metrics_summary_`. |
| `missing` | what the store leaves out, and why |

Every Iceberg table carries the same `license`, `citation` and `source` in
its table properties, so a table read on its own still attributes. The
dataset titles, page URLs and publication dates come from the dataset pages,
which block automated reads. The ingest takes them from the hand-written
record in `LAYOUTS.md`, "Attribution", which a person checks against the
page. The `sample` attribute also carries what the page states: species,
anatomical entity, preservation method, disease state, the biomaterials and
the protocol document numbers.

**The bundle.** Each `outs.zip` stores all 23 members with no compression, so
every member is a byte range of the object. The members, with the kidney's
sizes:

| Member | Format | Size | In the store |
|---|---|---|---|
| `morphology_2d/ch000{0..3}_*.ome.tif` | 4 OME-TIFF pyramids, one channel each, 8 or 9 levels, 1024 tiles, lossless JPEG 2000, `uint16` | 7.0 GB | `images/morphology`, decoded, zstd |
| `morphology_3d/ch0000_dapi_3d.ome.tif` | one OME-TIFF pyramid, 13 or 15 planes, same tiling and codec | 20.8 GB | `images/morphology_3d` |
| `cells.zarr.zip` | Zarr v2 "CellSegmentationDataset 6.3": two `uint32` masks, two polygon sets, a gridded polygon copy, a cell summary | 0.5 GB | `labels/*`, `shapes/*`, chunks copied |
| `cell_feature_matrix.zarr.zip` | Zarr v2 AnnData: CSR `X` of `int32`, `obs`, `var`, `obsm` | 1.3 GB | `tables/table`, chunks copied |
| `csc_cell_feature_matrix.zarr.zip` | the same matrix by column | 1.1 GB | out: a copy of `X` |
| `transcripts.zarr.zip` | Zarr v2 grid of 30 to 37 tiles, six column arrays each | 12.8 GB | `points/transcripts`, as an Iceberg table and a `uint8` copy |
| `binned_transcripts.zarr.zip` | per-gene 2D bins for the viewer | 2.2 GB | out: derived from the transcripts |
| `diffexp_genes/*.csv` | two tables, Azimuth and graph clustering | 36 MB | Iceberg tables |
| `experiment.spatial`, `metrics_summary_*.csv`, `*_fov_locations.json` | JSON and CSV | 25 kB | root attributes |
| `panel_config.json`, and the transcripts store's root attributes | JSON, 11 MB and 2.4 MB: per-feature and per-codeword records | 14 MB | `var` columns where per-feature; the `codewords` Iceberg table otherwise |
| `analysis_summary_*.html`, `qc_images.html` | single-page apps with one JSON data bag each | 15 MB | the QC rasters as images; the rest out |
| `overview_scan.png` | the camera scan | 9 MB | `images/overview_scan`, if it places (call 6) |

Nothing in the bundles is deflated. The dev previews of 2026-04 deflated their
imagery; those are out of scope.

**Formats inside the zips.** The Zarr v2 stores use blosc with `blosclz`,
`lz4` or `zstd`. Zarr v3's blosc codec reads those bytes unchanged, so every
chunk copies byte for byte with new metadata. The virtual stores
`earthmover-demos/atera-kidney-virtual` and `atera-cervical-virtual` read
these bundles' chunks that way today.

**How people use it.** 10x Explorer reads the bundle as published. In Python,
`spatialdata_io.xenium` reads the Xenium-family bundles into SpatialData. The
scverse and squidpy tutorials work on that object. zax-spatialdata will read
the store from Icechunk and the tables from Iceberg.

## Proposed layout

### Spec rules that constrain it

- SpatialData container format 0.2: `images/`, `labels/`, `points/`,
  `shapes/`, `tables/` at the root, each element with `spatialdata_attrs`
  and `coordinateTransformations` into a named coordinate system.
- Raster elements are OME-Zarr 0.5 multiscales. A 2D image is `(c, y, x)`;
  a 3D image is `(c, z, y, x)`; labels are `(y, x)`.
- Points and shapes are Parquet in the element group. The spec gives no slot
  for a table outside the store, so the `iceberg_table` attribute is
  minus-80's own, beside `spatialdata_attrs`.
- A table states its region, its region key and its instance key, and the
  instance key matches the labels' integer values.

### The tree

One repo per sample, `atera-kidney`, `atera-brain`, `atera-colorectal` and
`atera-breast`. One Iceberg namespace per repo, `<repo>-tables`.

```
/                                   spatialdata_attrs; sample, run and panel records; metrics; source objects; missing
images/morphology                   (c, y, x) uint16, 4 channels, 8 or 9 levels, 0.2125 µm, zstd
images/morphology_3d                (c, z, y, x) uint16, 1 channel, 13 or 15 planes at 3 µm, 8 or 9 levels, sharded
images/he_image                     (c, y, x) uint8 RGB, 7 or 9 levels, 0.2738 µm, Affine into global
images/overview_scan                (c, y, x) uint8 RGB, low resolution, Affine into global (call 6)
images/qc_transcript_density        (c, y, x), one channel, 20 µm bins, Scale then Translation into global; the dtype of the report (uint16 in kidney and brain, uint32 in colorectal and breast)
images/qc_mean_q_score              same grid, the dtype of the report (uint16 in the kidney)
images/qc_mean_q_score_80um         80 µm bins, the dtype of the report (uint16 in the kidney)
images/qc_negative_control_codewords  same grid as the density map, uint16 in the kidney
images/qc_negative_control_probes   same grid as the density map, uint16 in the kidney
images/qc_genomic_controls          same grid as the density map, uint16 in the kidney
labels/cell_labels                  (y, x) uint32, chunks copied
labels/nucleus_labels               (y, x) uint32, chunks copied
shapes/cell_boundaries              iceberg_table + shapes.parquet as a uint8 array
shapes/nucleus_boundaries           iceberg_table + shapes.parquet as a uint8 array
shapes/region_outline               the ROI outline from the overview scan, one polygon (call 6)
points/transcripts                  iceberg_table + points.parquet/part.<i>.parquet, one part per source tile
tables/table                        AnnData: X CSR int32, obs, var, obsm; region cell_boundaries, instance key instance
```

The ingest does not write `images/overview_scan` and `shapes/region_outline`.
The root attribute `missing` says why.

The root attribute `iceberg_tables` pins `codewords`, `diffexp_azimuth` and
`diffexp_graphclust`. These tables annotate no element. Each entry has the keys
of an `iceberg_table` attribute, without `geometry_column`. The store has no
group for them. The `reading` attribute says where they are.

Iceberg tables in `<repo>-tables`: `transcripts`, `cell_boundaries`,
`nucleus_boundaries`, `codewords`, `diffexp_azimuth`, `diffexp_graphclust`.
`codewords` has one row per codeword, 53,385 of them, with its feature index
and the nine category flags. So `feature_name` in the transcripts table has a
source a reader can check. The root attributes stay under 30 kB, because a
reader fetches the snapshot, and the snapshot holds them, on every open.

The root attribute `description` gives each kind of element that the store
holds: `images/morphology_3d`, `images/he_image`, the QC rasters and
`iceberg_tables`.

### Axes, units, levels

- The coordinate system `global` is in micrometres. The morphology pixel grid
  maps into it by `Scale(0.21249222)` on y and x. Labels use the same scale.
  The 3D stack adds `Scale(3.0)` on z.
- The alignment CSV's affine maps H&E pixels to morphology pixels. The H&E
  maps into `global` by that affine, then `Scale(0.21249222)`, the morphology
  pixel size. The matrix stays in the element's attributes as published.
- The QC rasters map by `Scale(20.0)` and `Translation(-10.0, -10.0)` from
  the report's `origin_data`, `dimensions` and bin count: 603 bins over
  12,060 µm for the kidney.
- Each image keeps every level the source has. The decoded level `i` of the
  store is the decoded level `i` of the source, so the pyramid equals 10x's.
  Level scales derive from the level shapes, as `minus80.ome.image_attrs`
  does.
- Chunks: `(1, 1024, 1024)` on 2D images, the source's tile. Labels keep the
  source's chunk shape, `(518, 1771)` for the kidney, because their chunks
  copy byte for byte. The 3D stack uses shards of `(z, 1024, 1024)` with
  inner chunks of `(1, 1024, 1024)`, as the zebrafish repo shards along z.
  The H&E takes the page's tile size as its chunk.
- Codec for decoded images: blosc with zstd and shuffle. Copied chunks keep
  the source's blosc settings.
- Channel names from the OME-XML: DAPI, ATP1A1/CD45/E-Cadherin, 18S,
  alphaSMA/Vimentin. The H&E's channels are r, g, b.

### Referenced, written, beside

| Content | Where |
|---|---|
| images, labels, table | written natively in the repo |
| transcripts, polygons | Iceberg tables beside the repo, and the same Parquet files as `uint8` arrays in the repo, by virtual reference to the table's data files |
| differential expression | Iceberg tables beside the repo |
| run, panel, metrics, FOV locations, source objects | root attributes |
| `csc_` matrix, `binned_transcripts`, HTML reports, thumbnails and plots | out; the root attribute `missing` says why |

### The tables

Transcripts, one row per decoded transcript, from the source tile arrays:

| Column | Type | From |
|---|---|---|
| `instance` | int64 | row position in the element, 0-based |
| `grid_tile` | string | the source tile key, the partition column |
| `x`, `y`, `z` | float32 | `location`, in micrometres |
| `codeword_index` | int64 | `codeword_identity`, widened from uint32 |
| `feature_name` | string, dictionary | `codeword_index` through the codeword map in the store's attributes |
| `cell_prefix`, `dataset_suffix` | int64 | `cell_id`, widened from uint32; the pair 4294967295, 4294967295 is an unassigned transcript, kept as published |
| `cell_id` | string | the Xenium form of the pair, as `spatialdata_io` builds it |
| `quality_score` | float32 | `quality_score`, widened from float16 (call 3) |
| `overlaps_nucleus` | bool | `overlaps_nucleus` |
| `bbox` | struct of four float64 | the point itself, for file pruning |

Every `uint32` source column is `int64` in both tables. Iceberg has no unsigned type. pyiceberg maps `uint32` to the signed `int`. 10x's sentinel 4294967295 overflows that type.

The ingest builds each tile in row slices and writes them as row groups with one `ParquetWriter`.
So the memory of a tile does not grow with its rows. `LAYOUTS.md`, item (f), gives the peak.

Polygons, one row per cell that has a polygon in the set, from `polygon_sets/<i>`:

| Column | Type | From |
|---|---|---|
| `instance` | int64 | the mask label, `cell_index + 1`, unique, the index column |
| `cell_index` | int64 | `cell_index`, as published, widened from uint32 |
| `cell_id` | string | the Xenium form |
| `part_count` | int32 | the number of source rows of the cell |
| `methods` | string | the parts' `method` numbers joined by commas in part order, with the method names in the table properties |
| `geometry` | binary, WKB | `vertices[:num_vertices]` in micrometres: a `Polygon` for one part, a `MultiPolygon` for several, in source order |
| `bbox` | struct of four float64 | the geometry's bounds |

A cell with no polygon has no row. So `instance` never shifts.
An invalid ring stays as published.
`LAYOUTS.md`, "The polygon sets", gives the kidney's counts.

The shapes tables carry the `geo` property with GeoParquet 1.1 metadata.
Their Parquet files carry the same `geo` key in the footer. The transcripts
table has no geometry column and no `geo` key. Its rows have plain `x`, `y`,
`z` and `bbox` columns. Every Parquet file carries the pandas index metadata
in its footer. That is possible because the ingest writes the files with
pyarrow and registers them with pyiceberg `add_files`. zax reads the footer,
and the shim reads the index.

The `iceberg_table` attribute on each element group:

```json
{"catalog": "arraylake", "org": "earthmover-biology", "namespace": "atera-kidney-tables",
 "table": "transcripts", "table_uuid": "<uuid>", "snapshot_id": 0,
 "index_column": "instance", "geometry_column": "geometry"}
```

`geometry_column` is present on shapes only. The ingest writes the table first
and the attribute after, so one repo commit pins one table snapshot. zax reads
Iceberg when the attribute is present and ignores the `uint8` array; a wrong
UUID or snapshot is an error there, not a fallback.

The `uint8` arrays carry the two `file:` convention attributes that the
virtual_omics shim's `declare_file` sets, because that shim is what reads them.
The ingest writes those attributes itself. It does not depend on the
`zarr_files` package. Their chunks are virtual references of 8 MiB into the
table's data files in the namespace's bucket. Arraylake runs no snapshot
expiration and no garbage collection for Iceberg tables (its catalog docs,
"Snapshot expiration"). So only a caller can delete a data file. The ingest
never expires or compacts these tables. A rewrite replaces a table's files in
one overwrite snapshot, and the old files stay in the bucket. Each table's
properties give the repo that references its files.

The name of each data file holds a hash of its bytes. The ingest never
overwrites an object, so other bytes never take a referenced name. A rerun
that writes the same bytes reuses the object. Each virtual reference also
carries the file's last-modified time. Icechunk refuses to read a file that
changed after that time.

Each data file carries the Iceberg field ids of the table schema, so a reader
needs no name mapping. The `instance` column is required in each table.

The `uint8` copies are never real chunks. If Arraylake's catalog refuses
`add_files`, the tables take pyiceberg's own `append`, whose files lack the
footer metadata, and the copies reference those files. The shim then takes
the `geo` metadata from the table property and the index column from the
`iceberg_table` attribute, which is what zax reads already. If the repo
cannot reference files in the R2 bucket at all (call 12), the copies are
dropped, points and shapes are Iceberg-only, and the shim scans the table.
The fixture on Arraylake used neither fallback: the catalog accepted
`add_files`, and the repo references the files through its container.

**Held tables.** The flag `--replace-tables <names>` lets the full ingest replace the rows of a table that the catalog holds.
The kidney's run used `--replace-tables transcripts,nucleus_boundaries`, because the `fixture` branch pins both.
The flag accepts every table that the full mode writes, and needs the element of each table in the run.
Without the flag, the ingest stops before any write on a held table that differs.
A schema change runs in one commit at the start of the run, before that table's rows.
One overwrite snapshot then replaces the rows. Each table keeps its name, its namespace and its UUID.
The ingest keeps a held `transcripts` tile that passes its row count and its `instance` range.
It replaces a failing tile in the overwrite snapshot that adds the tile's file.
It removes a held tile outside the plan in a snapshot of its own.
A write to a table that holds a file of other content raises. So no stop mixes the rows of two writes.
A rerun with the flag ends with the table of an unstopped run. A second run changes nothing.
The ingest deletes no snapshot and no data file. So the pins of the `fixture` branch stay valid.
pyiceberg scans a snapshot with the schema of that snapshot.
`ingest.py --mode full --preflight` checks the arguments, the repo, the catalog and each table. It needs no bundle and writes nothing.

**The table's region.** On `main`, `tables/table` annotates `cell_boundaries` only.
It has one row per cell: 399,120 rows for the kidney. Its `region_key` is `region` and its
`instance_key` is `instance`. `instance` equals the cell mask label.
`labels/cell_labels` and `shapes/nucleus_boundaries` join to a table row by
the same `instance`. So a join from the table to the shapes or the labels
never shifts by one. A SpatialData table row annotates one instance of one
element. So a table on two elements would need every row twice.
The fixture's table annotates `cell_labels`.
The gate `table_region` checks that the set of `obs.instance` equals the set of `cell_boundaries.instance`.
It also checks that `cell_id` agrees for each instance.

Shapes tables carry an `instance` column that equals the cell mask label. It
is the `index_column`. The source's `cell_index` stays as a column.

### Layouts considered and rejected

- **Virtual references into `s3://10x.files`.** Rejected: 10x publishes the
  bundles there as downloads. A store of references would send every
  reader's range reads to that bucket. The virtual repos in `earthmover-demos` stay
  as they are.
- **Points and shapes as `uint8` arrays only.** Rejected: zax reads such an
  array whole and refuses one above 256 MiB. The transcripts are 13 to 49 GB.
- **Points and shapes in Iceberg only.** Rejected: stock Python SpatialData
  cannot open the element, and the zax parity checks fail on it.
- **JPEG 2000 tiles copied byte for byte.** Rejected: exact and cheap, but
  Booth and the web app have no `imagecodecs`, and the released zax has no
  JPEG 2000 codec. The cellpainting repo has this problem today with LZW.
- **One Iceberg namespace `atera` for all samples.** Rejected for the org's
  precedent of one namespace per repo.
- **The HTML reports as `file:` arrays.** Rejected: the owner decided that
  `file:` arrays hold only the Parquet copies. The reports' data bag reduces
  to attributes and the QC rasters.
- **One table that annotates `cell_labels` and `cell_boundaries`.** Rejected:
  a SpatialData table row annotates one instance of one element. Two elements
  would need every row twice.
- **Stacking the four 2D channels from four files into one array.** Adopted:
  the four files are one OME-TIFF series, and `(c, y, x)` is the spec's shape.

## What has been tested

All on 2026-11-10, from a laptop, with range requests only.

- **Objects.** All sixteen objects answer HEAD with size, ETag and date. The
  dev previews' breast and cervical H&E objects share one ETag, so they are
  one file under two names; both are out of scope.
- **Zip directories.** Each bundle's central directory reads in 4 GETs. 23
  members, all stored, in all four bundles.
- **Inner stores.** The kidney and brain bundles' five Zarr zips were
  indexed in full: every member stored, every array's metadata read. The
  transcripts store has 30 tiles (kidney) and 35 (brain, by the survey) of six arrays each.
  The counts store's `X` is CSR with one-million-element chunks. The masks
  are `(33139, 56644)` and `(42548, 61369)` `uint32` in blosc-zstd.
- **Images.** The ten morphology TIFFs were parsed: 8 levels, 1024 tiles,
  `uint16`, compression 34712. One tile's codestream parsed as 5/3
  reversible, one layer, five decomposition levels: lossless. The two H&E
  files differ by version: kidney interleaved RGB with Deflate, 7 levels;
  brain planar RGB uncompressed, 9 levels.
- **Reports.** The kidney's two HTML reports were parsed. The analysis
  summary's data bag is 5.5 MB: about 100 kB of metrics and tables, 2.7 MB
  of JPEG thumbnails, 1.3 MB of QC rasters, 1 MB of plot data. The QC
  rasters are numeric, not pictures. Each is a base64 zlib stream with the
  dtype in its header. All six rasters of the kidney are `uint16`: the
  density map and the control maps, and also the quality maps. The ingest
  accepts `uint16`, `uint32` and `float32` and stores the dtype of the report.
  The report states each grid, its origin and its unit beside the rasters.
  The sum of the density map is 1,083,945,548. The report states
  1,083,799,964 high quality transcripts. The transcript store holds
  1,186,813,834 transcripts. The ingest does not claim what the map counts.
- **Metadata files.** `experiment.spatial`, the alignment CSVs,
  `metrics_summary.csv`, `panel_config.json` and both FOV location JSONs
  were read. The alignment is a 3 by 3 affine with a scale of 1.289 and a
  90-degree rotation.
- **Arraylake.** The org's bucket configurations, repos and the one Iceberg
  namespace were listed. The org's three bio repos were walked for codecs
  and chunking; the cellpainting repo fails to open without `imagecodecs`.

Tested in the fixture step, on 2026-11-10 and 2026-11-11, from a laptop:
the JPEG 2000 decode; writing a table through `add_files` on Arraylake's
catalog; virtual references from an Arraylake repo into the R2 bucket; the
shim reading the `uint8` copies on Icechunk. `LAYOUTS.md`, "Fixture
findings", gives the results. `write_page` and the `images` gate both decode
with tifffile, which calls `imagecodecs`. So the gate shows
that the stored pixels equal tifffile's decode. It does not compare two
decoders.

Tested on 2026-11-11: zax reads the fixture's Iceberg tables on the R2
endpoint (`LAYOUTS.md`, check (d)). Decode and write throughput on a Coiled
VM in us-west-2 (step 2; `LAYOUTS.md`, check (e) and item (f)).

Measured on 2026-11-11 with `measure.py` (step 2), on an `m7g.xlarge` (Graviton, 4 vCPU, 16 GiB) in us-west-2:

- Concurrency 32 gives 8.0 times the rate of concurrency 1 for the plane, 17.8 for the mask, 7.2 for the H&E and 1.5 for the stack.
- The stack's rate does not rise above concurrency 8.
- The peak memory of each part is under 1.3 GiB. Transcript tile `3,1`, 90,630,655 rows, peaked at 1,299 MiB.
- That tile's Parquet file is 2,230,658,968 bytes: 24.6 bytes per row.
- The machine token can write to the repo.
- The estimate from these rates was 8,854 s for the kidney's measured kinds. `REPORT.md`, "Changes from the plan", compares it with the run.

`LAYOUTS.md`, item (f), has every number.

Tested on 2026-11-11 and 2026-11-12, on a laptop with synthetic bundles, local repos and local catalogs:
the full mode, a stop and a resume, and the trial-branch flow.
`LAYOUTS.md`, "The dry run of the full mode" and "The dry run of the trial-branch flow", give the results.

Tested on 2026-11-12 on a Coiled VM: the survey of the four bundles (`LAYOUTS.md`, "The four bundles"),
and the gates `transforms` and `labels` on the kidney's `main` (`REPORT.md`, "What the gates do not prove").

## Judgement calls

**Decisions of the owner.**

- Native copy, not virtual references. 10x publishes the bundles as downloads
  in `s3://10x.files`, and a store of references would send every reader's
  range reads there.
- Four samples, smallest bundle first: kidney, brain, colorectal, breast.
- Org `earthmover-biology`, with the org's default managed bucket (Cloudflare R2).
- Points and shapes in Iceberg tables, with the `iceberg_table` attribute
  contract agreed with the zax-spatialdata project, and `uint8` copies in the
  repo. No `file:` arrays anywhere else.
- Images decoded once from their lossless JPEG 2000 tiles and written with
  blosc-zstd, chunked like the `zebrafish-3d-4i` repo. Booth and the Arraylake
  web app cannot read `imagecodecs_jpeg2k`.
- The QC density rasters from the HTML report become small image elements.
  The reports' thumbnails, plots and UI stay out.
- The ingest runs on Coiled in us-west-2, next to the source bucket.
- On `main`, `tables/table` annotates `cell_boundaries` only (2026-11-11).
- The survey of the bundles runs on Coiled, not on a laptop.
- Each sample's run writes a trial branch first. `main` moves to that snapshot after every gate passes.
- The plan's author creates each repo, tables namespace and access policy with the Python client.
  The owner approves each one first.
- The layout of `main` stays as the kidney has it. The kidney's data got one later commit, for the root attribute `description` only.

1. **Mask order.** The cells store gives its polygon sets the names `["nucleus",
   "cell"]` and its masks the names `0` and `1`. The order is not stated to match.
   Decided: `masks/0` is nucleus and `masks/1` is cell. Evidence: the
   fixture's `masks` gate sampled 50 nucleus centroids. `masks/0` label areas
   match `cell_summary.nucleus_area`, and `masks/1` label areas match
   `cell_area`, both with a median relative error of 0.000. The crossed pairs
   differ by 0.502 and 1.009.
2. **Instance key.** The masks hold integer labels. `cell_summary` holds one
   row per cell. The AnnData `obs` has `cell_id` as int64 and `barcode` as a
   string. The cells store has `cell_id` as a `(prefix, suffix)` pair.
   Decided: the mask label is the instance key. Evidence: the fixture's
   `instances` gate sampled 50 rows. On all 50, the cell mask at the centroid
   is the row position plus one. `obs.cell_id` is not the label. It packs the
   pair as `prefix + suffix * 2**32`. So the ingest adds `obs/instance`, the
   row position plus one, and `obs/region`. `obs` keeps `cell_id` and
   `barcode`. Decided on 2026-11-11: on `main`, the table annotates
   `cell_boundaries` only, and the labels and nuclei join by `instance`.
   "The tables" gives the reason.
3. **`float16` quality scores.** Parquet's FLOAT16 type is young, and zax's
   Arrow support for it is unverified. Recommended: `float32` in the tables,
   recorded in the table properties and in `LAYOUTS.md`.
4. **Micrometres as the global frame.** `spatialdata_io.xenium` puts images
   under `Scale(pixel_size)` and leaves points and shapes in micrometres.
   Recommended: the same, so the store reads like a Xenium one.
5. **The H&E as a transform.** Resampling would decode and rewrite an
   estimated 3 to 7 GB per sample and lose the original grid. Recommended:
   keep the pixels, place with the affine, keep the matrix in attributes.
   The ingest decodes the H&E tiles and writes them with the image codecs.
   The kidney's Deflate tiles have no codec in the Zarr core.
   One rule for all four samples is simpler than two.
   This is the plan author's call. The owner checks it.
6. **The overview scan.** Its FOV boxes are in scan pixels; the morphology FOV
   boxes are in micrometres; both JSONs list the same FOV ids. A least-squares
   affine over 78 boxes places the scan. Recommended: include it, and the ROI
   outline with it, when the residual is under one morphology pixel; else
   leave both out and say why.
7. **The 3D stack.** 19 to 23 GB per sample as JPEG 2000, about twice that as
   zstd. It is the only 3D data, and the virtual kidney store had it.
   Recommended: include it.
   The stack keeps shards of `(z, 1024, 1024)`. The ingest writes one shard at a time.
   It decodes one tile from every plane, then writes the shard once.
   A plane by plane write rewrites each shard once for each plane.
   The decode of one position runs in up to 4 threads on an `m7g.xlarge`: the plane count or the CPU count, whichever is less.
   This entry holds no measurement of the stack without shards.
   This is the plan author's call. The owner checks it.
8. **Transcript partitioning.** The source tiles are a spatial grid already.
   Recommended: one partition per tile, with the `bbox` struct so a window
   query prunes files by statistics.
9. **Differential-expression CSVs.** `var` already holds Leiden columns.
   Recommended: tables for the two CSVs, which use Azimuth and graph
   clustering, not Leiden.
10. **Namespaces.** Recommended: `<repo>-tables`, the org's precedent.
11. **`AGENTS.md`.** Its rules serve zero-copy: "never decode pixels",
    "match the source's chunking", "a `source_url` on every virtual array".
    Recommended: a short "native ingests" section that states when a copy
    is right, which rules it relaxes, and that the source's bytes must still
    verify against the store.
12. **Virtual references for the `uint8` copies.** They point into our own
    bucket, not a public one, so the repo's virtual chunk container needs
    Arraylake's credentials. The copies are never real chunks. Decided: keep
    the copies as virtual references. Evidence: an org admin added a private
    Virtual Chunk Access Policy on the org's managed bucket for the
    namespace's storage location. The repo's container is that location, so it sits at or under
    the policy's location. Without the policy, Arraylake refuses to open the
    repo, with HTTP 400. With it, the fixture's `copies` gate passed: each
    copy's bytes equal its Parquet file. The shim read both copies through
    Arraylake with no credentials of its own. Each sample's repo needs the
    same policy for its own namespace.
13. **Tables that annotate no element.** The plan's author made this call.
    The owner checks it. The codewords and the two differential expression
    tables sit in the namespace. The root attribute `iceberg_tables` pins
    them, each as an `iceberg_table` attribute does. Reason: SpatialData has
    no element type for a table that annotates no element. A root attribute
    adds no node that a SpatialData reader must skip. The index column is
    `codeword_index` for `codewords` and `Feature ID` for the other two.
14. **The OME version of the raster elements.** The plan's author made this
    call. The owner checks it. Decided: keep `0.5-dev-spatialdata`.
    SpatialData requires that string, and `AGENTS.md` asks for OME-Zarr 0.5 metadata.
    Evidence from 2026-11-11: `ome-zarr-models` 1.8.1 rejects the version string and
    nothing else, and accepts the attributes with `0.5`. SpatialData 0.8.0
    parses the stored transforms and gives the expected points. Booth is not checked.
    `LAYOUTS.md`, "The OME version check", gives the text.

**Levels above 0.** `level_scale` gives each level a scale that sets its pixel size.
It gives no translation. The plan's author made this call. The owner checks it.

**Calls on the flow of each run.** The plan's author made these calls. The owner checks each of them.

- The remote script moves `main` by itself when every gate passed. It does not wait for a person.
- A failed gate leaves `main` where it was and keeps the trial branch for a look.
- The machine is an `m7g.xlarge` with `--concurrency 32`. The launcher sets the disk from the sample's download size.
- The kidney's `description` commit ran on a VM. It read the bundle by range request and wrote the root attributes only.
- The QC rasters' decode has no second decoder. `REPORT.md` keeps that limit.
- The survey writes one record file for each sample, `samples/<sample>.json`. The bundle check compares a bundle with that file.
- The bundle check runs before the download, by range request. It runs again on the local file before the ingest.
- A rerun on the same trial branch resumes. A fix that changes written arrays uses another trial branch name.
  A fix that changes a table's rows uses `--replace-tables`. No run deletes a branch, a table or an object.
- `main` moves only forward: its snapshot must be an ancestor of the trial snapshot.
- The plan's author runs the setup script with the owner's login. The machine token has no right to set a policy.

## Steps, scope and risk

Each step needs approval before it writes to a shared resource.

1. **Fixture, kidney, branch `fixture`.** Create the repo `atera-kidney` and the
   namespace `atera-kidney-tables`. Write level 6 of `images/morphology`, the
   two masks' lowest pyramid level, one transcript tile as a table and a
   `uint8` copy, the nucleus polygons as a table and a copy, and the whole
   AnnData table, `X` included. Then run these checks:
   - Open it with the virtual_omics shim, in that project's `spatial` environment.
   - Open it with stock SpatialData 0.8.
   - Send its coordinates to the zax-spatialdata project for its Iceberg source and tests.
   - Check the R2 virtual references.
   - Check `add_files` on the catalog.
   - Compare the stored tiles with tifffile's decode.

   Pass criteria: the shim opens every element. Stock SpatialData opens the
   images, the labels and the table. It reports the points and shapes as
   absent. That is the expected limit. Approval needed: creating one repo and
   one namespace in `earthmover-biology`.

   The ingest itself writes with zarr, icechunk, pyarrow and pyiceberg
   through `minus80`, and never imports spatialdata. It builds the
   SpatialData attributes from the spec, as `minus80.ome` builds OME-Zarr's.

   Result: the fixture ran on a laptop, on 2026-11-10 and 2026-11-11.
   Stock SpatialData 0.8.0 cannot open an Icechunk store at all, so that criterion failed.
   `LAYOUTS.md`, "Fixture findings" and "The rewrite", give the results.
2. **Throughput on Coiled.** `measure.py` measures the decode and the write of each kind on one VM in us-west-2.
   It writes to a scratch branch that does not remain.
   Result: "What has been tested" gives the rates. They set the machine and the concurrency of step 4.
3. **Docs change.** The `AGENTS.md` section (call 11), and a `formats.md`
   note on JPEG 2000 in OME-TIFF: lossless 5/3 tiles decode once with
   `imagecodecs`; Booth cannot read the codec. Approval needed: the rules
   change.
4. **Kidney, full.** One run of the launcher's ingest form, as "The run of each sample" gives.
   Approval needed: the Coiled spend and about 100 GB of R2 storage.
   Result: the run ended on 2026-11-12 with all 16 gates passed.
   A later run wrote the root attribute `description` on a trial branch and moved `main`.
   `REPORT.md`, "Timings" and "The description commit", give the facts.
5. **Brain, colorectal, breast,** each as step 4, smallest first.
   The survey of step 5a comes first.
   - **5a. The survey.** `survey.py` reads the metadata of each bundle by range request on one VM.
     It downloads no bundle, writes nothing to Arraylake, and reads no pixel, transcript column or matrix chunk.
     It writes each record as `samples/<sample>.json`. The kidney is a control: its record must equal `LAYOUTS.md`.
     `LAYOUTS.md`, "The four bundles", gives the facts and the differences from the kidney.
   - Approvals of each sample: the setup on Arraylake, the Coiled spend, the storage and the run.
   - Result: all three runs passed every gate and moved `main`.
     `REPORT.md`, "The brain sample" and "The colorectal and breast samples", give the facts.
6. **Notebook** that opens one store cold, per `AGENTS.md`.

### The run of each sample

**The setup on Arraylake.** `setup_sample.py --sample <sample> --org <org>` sets up four pieces:
the repo, the tables namespace, the virtual chunk container and the access policy.
It reads the namespace's location from the catalog at run time. No file holds it, and the script prints no bucket name and no prefix.
It first checks the bucket: the org's default bucket must be the kidney's bucket.
The sample's repo and namespace, where they exist, must be on that bucket too.
Each piece prints `exists`, `created`, `would create` or `DIFFERS`. A piece that differs stops the script with exit code 1.
`--dry-run` reads the org and creates nothing.
The script refuses a public or wider policy. It refuses a prefix segment that is empty, `.`, `..` or holds a space.
Run the dry run just before each launch. It must print four `exists` lines.
The ingest writes to the catalog before it checks the virtual chunk container.
So a missing piece fails about 30 minutes into the run.

**The command.** One VM runs each sample:

```
uv run --package atera-ingest --extra launch python ingests/atera/launch.py \
  --vm-type m7g.xlarge --region us-west-2 --workspace earthmover-devs --name atera-<sample> \
  --ingest --sample <sample> --repo arraylake://earthmover-biology/atera-<sample> \
  --catalog arraylake://earthmover-biology --namespace atera-<sample>-tables \
  --branch trial-<date> --concurrency 32 --max-minutes <limit> --token-file <path>
```

- `<date>` is the UTC date of the launch, as `YYYYMMDD`. The launcher uses that name when `--branch` is absent.
- `<path>` is the file of the Arraylake machine token.
- `<limit>` comes from "The time limit".
- The command has no `--gates`. So the verification runs every gate of the full mode, and then `promote.py` moves `main`.
- The command has no `--replace-tables` when the catalog holds no table of the sample.
- The command has no `--disk`. The launcher sets the disk from the downloads.
- The launcher uploads the committed code of the local checkout, so the run needs no push.
- With `--dry-run`, the launcher contacts nothing, reads no token, and prints the disk and the job timeout.

The remote steps are environment, preflight, bundle-remote, space, download, check, bundle-local, ingest, verify and promote.
After the run, check that the cluster stopped. Then add the sample's section to `REPORT.md`.

**The disk.** The disk is the downloads in GiB, rounded up, plus 40 GiB of overhead.
The launcher rounds that up to a multiple of 25 GiB.
The overhead holds 10 GiB for the scratch Parquet file of the largest tile, 10 for the uv environment, 10 for the system, and 10 of margin.
The space step needs the downloads and 10 GiB free.
The largest tile is in the colorectal sample: 334,725,210 rows, about 8.2e9 bytes at 24.6 bytes per row. It fits the 10 GiB.
The disks were 100 GiB for the brain, 125 GiB for the colorectal and 150 GiB for the breast.

**The bundle check.** `facts.bundle_facts` reads every fact that a later step needs.
It builds the root attributes, resolves each member, reads the QC labels and the transcript grid, and opens each TIFF header.
`check_bundle.py` compares the facts with `samples/<sample>.json`. A difference is one line that gives the fact and both values.
Its exit code is 0 for no difference, 1 for a difference, 2 for a record that cannot serve, 3 for a bundle that does not open, and 4 for a check that did not finish.
Without `--bundle`, it also compares the size and ETag of each source object by a HEAD request.
The group `members` of the facts pins the size of each store member.
The launcher runs the check by range request before the download, and again on the local files.
`ingest.prepare` runs the comparison before the first write. The verification runs it as the gate `bundle`.
Each of the three scripts takes `--record` for a record file outside `samples/`. A record with an error entry stops each of them.
The ingest finds the members by pattern: `morphology_2d/ch*.ome.tif`, `morphology_3d/ch*.ome.tif`, `metrics_summary*.csv` and `analysis_summary*.html`.
A `*` matches inside one path segment. The patterns must match 4, 1, 1 and 1 members, or `ValueError` stops the step.

**Verification.** `verify.py` runs gate by gate, per `docs/ingest.md`.
It compares every image level against tifffile on a sample of tiles, and every copied chunk by checksum.
It compares every table's row count against the source arrays, and checks the shim and zax reads.
`REPORT.md`, "Verification", lists the gates and what each compares.
The gate `transforms` compares each element's transform with the source:

- each image and label level's scale, against the `experiment.spatial` pixel size and the level shapes;
- the stack's z step, against the OME-XML;
- the H&E affine and the `alignment` attribute, against the CSV, which the gate parses itself;
- each QC raster's scale and translation, against the report's bins and origin;
- where each stored transform maps the corners of each level: four of a plane, eight of the stack.

The gate computes each expected value from the source, not from the ingest's functions.
A stored number must equal its source number within a relative error of 1e-9.
The alignment CSV is a 3 by 3 matrix in `(x, y)` order. It maps H&E pixels to morphology pixels.
A far corner of each level must map to the far corner of level 0.
The gate binds each stored axis name to its array dimension, by the axis order that `LAYOUTS.md` records.
It reads the first multiscale of each element only.
It does not compare the input and output axes of a transform with the stored axes.

The gates `labels` and `matrix` also read 20 chunks through zarr, from the first chunk to the last.
Each must equal the source's decode by `zarr_copy.read_region`, which uses no chunk key. So a chunk under a wrong key fails.
At least one sampled chunk must hold a value other than the fill value.
The gate `matrix` also compares every chunk of `X/indptr`.
The gate `labels`, and the gate `matrix` in full mode, compare each metadata field with the source `.zarray`.
The gate `complete` checks that the `description` gives each element kind that the store holds.

**The trial branch, then `main`.** The launcher's ingest form writes `trial-<name>`, and refuses another branch.
A trial branch has the name `trial-<date>`, or `trial-<date>-<n>` for a later try.
`verify.py --snapshot-file` writes the snapshot that the gates read. It writes it only when at least one gate ran and none failed.
After the verification passes, `promote.py --expect` moves `main` to that snapshot.
It refuses another trial snapshot, so a later commit on the trial branch moves nothing.
It refuses when the snapshot of `main` is not an ancestor of the trial snapshot.
So `main` moves only forward, and the table pins of the verified snapshot stay as written.
The launcher refuses `--gates` unless the run has exactly one of `--no-promote` and `--promote-after-gates`.
With `--remote-bundle` the only element is `root`, and each gate is one of `complete`, `attrs` and `source`.
`ingest.py --root-keys` writes only the root keys that it gets. It needs `--elements root` alone and the full mode.
It prints each root key that changes. A change to another key stops the ingest before the write.

**The time limit.** The limit is twice the estimate, rounded up to a whole minute.
The estimate scales each step time of the kidney's run by the ratio of one quantity of the sample to the kidney's.
The kidney's times come from `REPORT.md`, "Timings" and "The description commit".
The quantities come from `samples/<sample>.json` and `LAYOUTS.md`.
The download and the file check scale by bytes. The bundle checks scale by the survey's reads.
The transcripts scale by rows. Each image and the labels scale by the pixels of level 0, and the stack also by planes.
The matrix scales by its stored values. The QC rasters scale by bins.
The verification scales by the largest ratio of a gate's input.
That is the pixels for the brain, and the stored values of the matrix for the colorectal and the breast.
A ratio below 1 keeps the kidney's time, because no run measured the fixed cost of a step.
A step with no quantity keeps the kidney's time: environment, preflight, `iceberg_tables`, the root and promote.
The kidney's ingest held no bundle check, so the estimate adds one for `ingest.prepare`.
The estimate counts no gain from the threads of the stack.
The rates of `LAYOUTS.md`, item (f), read from 10x's bucket. The run reads local files, so the estimate uses the run's times.

The worked example for the brain:

| Step | Kidney (s) | Ratio | Brain (s) |
|---|---|---|---|
| environment | 13 | 1 | 13.0 |
| preflight | 14 | 1 | 14.0 |
| bundle-remote | 77 | 0.7011 | 54.0 |
| space | 0 | 1 | 0.0 |
| download | 991 | 1.1414 | 1,131.1 |
| check | 377 | 1.1414 | 430.3 |
| bundle-local | 77 | 0.7011 | 54.0 |
| ingest: the bundle check of `ingest.prepare`, an added estimate | 77 assumed | 0.7011 | 54.0 |
| ingest: `tables/table` | 905.3 | below 1, so 1 | 905.3 |
| ingest: two labels | 69.9 | 1.3910 | 97.2 |
| ingest: two shapes | 30.8 | below 1, so 1 | 30.8 |
| ingest: `points/transcripts` | 1,749.3 | 1.3823 | 2,418.0 |
| ingest: `iceberg_tables` | 30.5 | 1 | 30.5 |
| ingest: six QC rasters | 17.9 | 1.3897 | 24.9 |
| ingest: `images/morphology` | 625.0 | 1.3910 | 869.4 |
| ingest: `images/morphology_3d` | 3,257.4 | 1.6050 | 5,228.2 |
| ingest: `images/he_image` | 68.5 | 1.5662 | 107.3 |
| ingest: root attributes | 1.6 | 1 | 1.6 |
| ingest: the rest | 72.8 | 1 | 72.8 |
| verify: the gates of the kidney's run | 2,246 | 1.3910 | 3,124.2 |
| verify: the gate `bundle` | 77 | 0.7011 | 54.0 |
| promote | 4 | 1 | 4.0 |
| Sum | | | 14,718.6 |

The ratios:

- Downloads: 55,377,843,512 / 48,518,883,775 bytes = 1.1414: the bundle, the H&E and the alignment CSV.
- Bundle check: 103,065,174 / 146,999,959 bytes = 0.7011, the survey's reads.
- Pixels of level 0: (42548 x 61369) / (33139 x 56644) = 1.3910. The masks have the shape of level 0.
- Stack: 15 / 13 planes x 1.3910 = 1.6050.
- H&E pixels of level 0: (45694 x 34152) / (42169 x 23628) = 1.5662.
- Transcript rows: 1,640,508,930 / 1,186,813,834 = 1.3823.
- QC bins of the 20 µm grid: (453 x 653) / (353 x 603) = 1.3897.
- The brain's matrix and polygons are smaller than the kidney's.

The estimate is 14,718.6 s, or 245.3 minutes. Twice the estimate is 490.6 minutes. The limit is 491 minutes.
The same rule gives 679 minutes for the colorectal and 786 minutes for the breast. The kidney's limit was 480 minutes.
The runs took 9,203 s, 10,957 s and 15,976 s. `REPORT.md` compares each step with its estimate.

**If the run fails.** A rerun resumes on the trial branch of the failed run, so it needs the same `--branch`.
Without `--branch`, the launcher uses `trial-<date>` with the UTC date of the launch.
So a rerun on another day gets another branch. Give the branch on every rerun.

A rerun starts on another VM, so it repeats the download, the file check and every gate.

The ingest commits each image level, and marks each image complete with the attribute `minus80:complete`.
The ingest skips each unit that the trial branch holds by a commit.
These units are `tables/table`, each label, each shape, `points/transcripts`, each image level, each QC raster and `iceberg_tables`.
The ingest skips a held transcripts tile when its row count and `instance` range are correct.
A held tile with another count stops the ingest, unless the rerun gives `--replace-tables transcripts`.
A table write with the same content as the held file changes nothing.

The launcher moves `main` after a part of the gates only with `--gates <list> --promote-after-gates`.
That form needs the owner's approval.

A failed run leaves these items behind:

- the trial branch and its chunks in the sample's repo;
- the tables of the sample's namespace, with the snapshots that the run wrote;
- the four pieces of the setup: the repo, the namespace, the virtual chunk container and the access policy;
- the logs of the job in Coiled.

The VM stops on every exit path of the script, and the server stops it at `--max-minutes`.
No gate verified the current snapshot of each table, so a reader of that snapshot sees those rows.
`main` of a new repo pins no table, so a reader of `main` sees no data of the failed run.
`REPORT.md`, "The colorectal and breast samples", gives the one failed run and its rerun.

### Shared-package work

Code that a second archive could use goes into `minus80`, with tests, and reuses what is there first:

| Need | Reuse | Add |
|---|---|---|
| Arraylake repos as targets | `minus80.core.icechunk_io.arraylake_repository`, on `main` | nothing |
| OME-Zarr 0.5 image, label and multiscale metadata | `minus80.ome.image_attrs`, `label_attrs` | the SpatialData element attributes: `spatialdata_attrs` and `coordinateTransformations` |
| A native pyramid whose every level comes from the source | `minus80.core.multiscale`, which writes a virtual level 0 and computed levels | a path that writes decoded source levels as real chunks, with shards |
| JPEG 2000 tiles out of an OME-TIFF | tifffile's `page.decode`, which calls `imagecodecs` | `minus80.core.pyramid`: it decodes each tile once and writes it. It is in `core`, not `parsers.tiff`, because it writes real chunks and builds no references. |
| Zarr v2 chunks inside a stored zip, inside a stored zip | `minus80.parsers.zip`, which addresses members as chunks | a v2-to-v3 metadata translation and a byte copy of each chunk |
| Iceberg tables with the `iceberg_table` attribute and the `uint8` copy | `minus80.core.icechunk_io.create_repo`'s virtual chunk containers | a `minus80.core.tables` module: write Parquet with pyarrow, register with `add_files`, pin the snapshot in the attribute, declare the copy |

`TableWriter`, `ElementWriter`, `ReadCounter` with `counting_reader`, and `read_array` are in `minus80.core`.
`minus80.core.zarr_copy.chunk_key` gives the key of a chunk, for the ingest and for the gates' hash comparison.

### Risks

- **Access policy per repo** (call 12). Each sample's repo opens only when
  an org admin's Virtual Chunk Access Policy covers its container. Each
  sample's step needs that policy before the ingest starts.
- **The Python read path** rests on unreleased spatialdata patches and the
  virtual_omics shim. The fixture showed it: released SpatialData 0.8.0
  cannot open an Icechunk store, and PR #1107 with its patches and the shim
  opens every element. Readers outside our notebooks need those until
  spatialdata PR #1107 or the `parquet_fs_fallback` hook lands.
- **Booth and OME-Zarr 0.5.** Whether Booth serves a SpatialData image
  element as tiles is untested. It does not change the store. The elements declare
  `ome.version` `0.5-dev-spatialdata` (call 14).
- **Node count.** About 15 images with 8 levels, 2 labels, 2 shapes and one
  table. The points have one part per source tile, 30 to 37 parts. That is a
  few hundred nodes per repo, far under the 500,000-node snapshot cache.
- **Tables outside the verification.** A reader of a table's current snapshot, not of a pin on `main`, can see rows that no gate verified.

### Open items

- **The trial branches.** Each of the four repos keeps its branch `trial-20261112` or `trial-20261112-description`.
  Each one is at the snapshot of `main`. A deletion needs the owner's approval.
- **The credentials of an upload.** `TableWriter` repeats an upload whose vended credential expired, up to three attempts.
  One upload must end within the 1,080 s of a new credential. `docs/upstream.md` gives the upstream fix.
- **The stain names inside the OME-XML.** The record holds the file names of the morphology members.
  It does not hold the channel names inside the files.
  The ingest takes the channel names from the OME-XML when it writes.
  So the bundle check finds a renamed stain file, but not a changed name inside a file.
  A record key for the names needs a second survey run of each sample.
- **Gates with the ingest's code.** The gate `qc` decodes the report with the ingest's decoder, `qc.bundle_rasters`.
  The gates `tables`, `polygons`, `masks` and `instances` decode `cell_index`, `cell_summary` and `cell_id` with `read_array`.
  The ingest decodes these arrays with the same function.
  The docstring of `verify.py` lists each use of the ingest's code. A defect in shared code can pass these gates.
- **The threads of the stack.** No run compared the stack with and without threads.
  `REPORT.md`, "The brain sample", gives the stack's time against its estimate.
- **The read limit of the bundle check.** `check_bundle.py` reads at most 200,000,000 bytes by default.
  The launcher gives no `--max-bytes`.
  The survey read 152.9 MB of the colorectal bundle and 169.8 MB of the breast bundle.
- **Calls for the owner to check.**
  - A person can still run `ingest.py --mode full --branch main` by hand. Only the launcher requires a trial branch.
  - The preflight does not test that `main` is an ancestor of the trial branch. `promote.py` tests it at the end.
- **Minor code items.** These smaller items wait:
  - `minus80.parsers.zip.nested`: `Window.seek` and its tests, and the zip that stays open.
  - `minus80.core.zarr_copy`: the refusals and their tests.
  - `minus80.core.pyramid`: `write_page` skips a missing tile silently; `_check_shape` checks only `y` and `x`.
  - `minus80.ome.spatialdata`: checks on the input of a transform.
  - `minus80.core.tables`: partial references after a failed write, and `require_table` on a table with no snapshot.
  - The ingest's tests: the conftest, and a close on `Bundle`.
  - `bundle.py` and `polygons.py`: tests of `read_array`, and the loop over polygons.
  - `ingest.py` and `verify.py`: `ReadCounter`, the lambdas, and repeated checks.
  - Prose fixes in `docs/upstream.md`.
