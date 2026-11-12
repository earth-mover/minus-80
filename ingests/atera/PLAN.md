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

Nothing is written to a shared resource before approval. The fixture, each
sample's full run, and the docs change each need approval in turn (see "Steps,
scope and risk").

Decisions already made, in discussion on 2026-11-10:

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
| `Atera_v1_WTA_Kidney_non_diseased_FFPE` | Atera In Situ Gene Expression, FFPE Human Kidney | 1.0.4 | 399,120 | 1,185,931,621 | 45.7 GB | 2.8 GB | 2026-10-15 |
| `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF` | Atera In Situ Gene Expression, Fresh Frozen Human Brain | 1.1.1 | 91,431 | 1,638,575,672 | 48.9 GB | 6.5 GB | 2026-11-03 |
| `Atera_v1_WTA_S4B_Colorectal_Cancer_FF` | Atera In Situ Gene Expression, Fresh Frozen Human Colorectal Cancer | 1.1.1 | 446,777 | 3,851,673,795 | 75.3 GB | 5.1 GB | 2026-11-05 |
| `Atera_v1_WTA_S4C_Breast_Cancer_FF` | Atera In Situ Gene Expression, Fresh Frozen Human Breast Cancer | 1.1.1 | 485,985 | 4,560,507,369 | 89.3 GB | 7.3 GB | 2026-11-05 |

The page URLs, the biomaterials, the protocol document numbers and each
object's size and ETag are in `LAYOUTS.md`, "Attribution". The published
dates are the pages' dates.

All four share the human whole-transcriptome panel `hWTA_v1` of 18,049 targets,
a pixel size of 0.21249222 µm, a z step of 3 µm, and the segmentation stain
set `atera_cell_segmentation_stains_v1`. The three fresh-frozen samples are
three regions of one slide, S4, from one run. The brain sample assigns 45
percent of its transcripts to cells; the others assign 84 to 88 percent.

**Licence and attribution.** The datasets are licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), which permits a
re-hosted copy with attribution. 10x's citation page gives one format for a
dataset, by example:

> PBMCs from C57BL/6 mice (v1, 150x150), Single Cell Immune Profiling Dataset
> by Cell Ranger v3.1.0, 10x Genomics, (2019, July 24).

So a dataset citation names the dataset's title, the product, the software and
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
| `morphology_2d/ch000{0..3}_*.ome.tif` | 4 OME-TIFF pyramids, one channel each, 8 levels, 1024 tiles, lossless JPEG 2000, `uint16` | 7.0 GB | `images/morphology`, decoded, zstd |
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
- Points and shapes are Parquet in the element group. The spec names no slot
  for a table outside the store, so the `iceberg_table` attribute is
  minus-80's own, beside `spatialdata_attrs`.
- A table names its region, its region key and its instance key, and the
  instance key matches the labels' integer values.

### The tree

One repo per sample, named `atera-kidney`, `atera-brain`, `atera-colorectal`,
`atera-breast`. One Iceberg namespace per repo, `<repo>-tables`.

```
/                                   spatialdata_attrs; sample, run and panel records; metrics; source objects; missing
images/morphology                   (c, y, x) uint16, 4 channels, 8 levels, 0.2125 µm, zstd
images/morphology_3d                (c, z, y, x) uint16, 1 channel, 13 or 15 planes at 3 µm, 8 levels, sharded
images/he_image                     (c, y, x) uint8 RGB, 7 or 9 levels, 0.2738 µm, Affine into global
images/overview_scan                (c, y, x) uint8 RGB, low resolution, Affine into global (call 6)
images/qc_transcript_density        (c, y, x) uint16, one channel, 20 µm bins, Scale then Translation into global
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
properties name the repo that references its files.

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

The full ingest replaces the rows of `transcripts` and `nucleus_boundaries` in place.
It needs the flag `--replace-tables transcripts,nucleus_boundaries`.
The flag accepts every table that the full mode writes. For a single-write table it means the overwrite path.
A refused write names the exact flag. The flag with a held element and another schema is refused before any change.
Without it, the ingest stops before any write on a held table that differs.
It also stops on a held `transcripts` tile that fails its checks, and the message lists every fault.
A write to a table that holds a file of other content raises. So no stop mixes the rows of two writes.
The flag needs the element of each named table in the run.
The ingest refuses the flag for a table whose element is not in the run.
The schema change runs at the start of the run, before that table's rows.
A stop between the two leaves the changed schema with the earlier rows at the current snapshot.
A rerun with the flag replaces the rows. A rerun with no flag stops.
`ingest.py --mode full --preflight` checks the arguments, the repo, the catalog and each table. It needs no bundle and writes nothing.
Both tables keep their names, their namespace and their UUIDs.
The fixture's `nucleus_boundaries` has a `method` column and several rows for one cell.
The full table has `part_count` and `methods` instead of `method`, and one row per cell.
One schema commit adds `part_count` and `methods` and deletes `method`. One overwrite snapshot then replaces the rows.
The ingest keeps a held `transcripts` tile that passes its row count and its `instance` range.
It replaces a failing tile in the one overwrite snapshot that adds the tile's file.
It removes a held tile outside the plan in a snapshot of its own.
A rerun with the flag ends with the table of an unstopped run. A second run changes nothing.
The ingest deletes no snapshot and no data file. Earlier snapshots and their files remain.
The `fixture` branch reads its pinned snapshots, and the pins stay valid.
pyiceberg scans a snapshot with the schema of that snapshot, so the `method` column stays readable.
The `uint8` copies of the `fixture` branch reference the earlier files. No run rewrites them.

Shapes tables carry an `instance` column that equals the cell mask label. It
is the `index_column`. The source's `cell_index` stays as a column.

The owner decided the table's region on 2026-11-11. On the Arraylake branch
`main`, `tables/table` annotates `cell_boundaries` only. It has one row per
cell: 399,120 rows for the kidney. Its `region_key` is `region` and its
`instance_key` is `instance`. `instance` equals the cell mask label.
`labels/cell_labels` and `shapes/nucleus_boundaries` join to a table row by
the same `instance`. So a join from the table to the shapes or the labels
never shifts by one. A SpatialData table row annotates one instance of one
element. So a table on two elements would need every row twice.

The fixture's table annotates `cell_labels`. "Stage two" lists the change to
`cell_boundaries` and its verify gate.

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
VM in us-west-2 (step 2; `LAYOUTS.md`, check (e)).

## Judgement calls

1. **Mask order.** The cells store names its polygon sets `["nucleus",
   "cell"]` and its masks `0` and `1`. The order is not stated to match.
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
    call. The owner checks it. Decided: keep `0.5-dev-spatialdata`. Evidence
    from 2026-11-11: `ome-zarr-models` 1.8.1 rejects the version string and
    nothing else, and accepts the attributes with `0.5`. SpatialData 0.8.0
    parses the stored transforms and gives the expected points. Stage two,
    item 6, gives the reason to keep the string. Booth is not checked.

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

   Where each part ran. All parts ran on a laptop outside us-west-2. The
   fixture on a local repo and catalog ran on 2026-11-10. The fixture on
   Arraylake ran on 2026-11-11. The decision is that the ingest runs on
   Coiled in us-west-2. The fixture ran on a laptop. `verify.py` and the
   SpatialData checks against Arraylake also ran on the laptop. The laptop's
   times do not predict the ingest. `LAYOUTS.md`, "Fixture findings", gives
   the results.

   Criterion failed: stock SpatialData 0.8.0 cannot open an Icechunk store
   at all.

   The zax-spatialdata project refused the fixture on three points: data
   files without field ids, the index of `obs` and `var` also in
   `column-order`, and an `instance` column that may be null. The rewrite on
   2026-11-11 fixed all three on branch `fixture`. `LAYOUTS.md`, "The
   rewrite", gives the new coordinates.
2. **Throughput on Coiled.** Done on 2026-11-11 with `measure.py`, on one
   `m7i.xlarge` (4 vCPU) in us-west-2, on the scratch branch
   `measure-2026-11-11`. All numbers below are from that VM. The 1-vCPU runs
   ran under `taskset -c 0`, which pins the process to one vCPU.
   - One full-resolution DAPI plane, 1,848 tiles, 1 vCPU: 1,204.8 s wall,
     284.5 s CPU for the whole process, 1.53 tiles/s, 2.97 MiB/s decoded.
   - The same plane on 4 vCPU: 911.2 s wall, 2.03 tiles/s.
   - One label mask, 2,048 chunks, 1 vCPU: 968.3 s wall, 2.12 chunks/s,
     0.12 MiB/s. This time holds both the source reads and the writes.
   - 955.3 s of the 1-vCPU plane is inside the array writes: 0.55 s per
     written chunk. The cause of the 0.55 s is not measured.
   - The process used 0.15 s of CPU per tile, for decode and write together.

   Expected time for one kidney sample with one serial writer, derived from
   the 1-vCPU rates. The second measurement below gives the estimate for the
   full run. The images are 4 2D planes and 13 3D planes. Three assumptions follow. The 3D
   planes have the 2D planes' size. Levels 1 to 7 add 655 tiles per plane,
   from the halved shapes in 1024-pixel tiles. A lower-level tile costs the
   same as a level-0 tile. So each plane has 2,503 tiles, and the sample
   has 42,551 tiles.
   - Images, one serial writer: 42,551 x 0.652 s = 27,740 s, 7.7 hours wall.
   - Masks, one serial writer: 2 x 968.3 s = 1,937 s, 0.5 hours wall.
   - Images and masks together: about 8.2 hours wall.
   - CPU for the images: 42,551 x 0.154 s = 6,550 s of CPU, 1.8 CPU hours.

   These figures leave out the table, the transcripts, the H&E image and
   the download. The 3D stack goes into shards of 13 planes. A plane-by-plane
   write of a shard can rewrite the shard. Step 2 did not measure that.

   Second measurement, with concurrent writes. Done on 2026-11-11 with
   `measure.py`, one Coiled VM for each launch. The VM is an `m7g.xlarge`
   (Graviton, `aarch64`, 4 vCPU, 16 GiB) in us-west-2. `launch.py` started it.
   It wrote to the scratch branch `measure-2026-11-11` with the Arraylake
   machine token. So the machine token can write to the repo. The branch does
   not remain. Each part measured a subset from the middle of its source, and
   passed its check. `LAYOUTS.md`, "Fixture findings", item (f), has every
   number. This is a different machine family from the first measurement, so
   the two runs have no ratio.
   - Rates at concurrency 1, 8 and 32 (tiles/s, chunks/s or positions/s):
     plane 1.615, 11.018, 12.982; mask 1.88, 15.04, 33.40; stack 0.294,
     0.439, 0.436; H&E 1.183, 6.419, 8.508.
   - Concurrency 32 against concurrency 1: 8.0 times for the plane, 17.8
     times for the mask, 7.2 times for the H&E, 1.5 times for the stack.
   - The stack decodes its planes in one thread. Its rate does not rise
     above concurrency 8.
   - Peak memory of each part: under 1.3 GiB. The largest, 1,299 MiB, is
     `plane` at concurrency 1 and the transcript tile.
   - Transcript tile `3,1`, 90,630,655 rows, to a local Parquet file:
     165.2 s, 548,733 rows/s, 72.7 s of CPU. The file is 2,230,658,968 bytes.
   - This step did not measure these items:
     - the download to the VM's disk;
     - reads from a local file;
     - the upload of Parquet files and the table commits;
     - the polygon tables;
     - the matrix copy;
     - the QC rasters;
     - `verify.py --mode full`;
     - levels above 0;
     - the checksum of a `uint8` copy on R2.
   - A staged create of a partitioned table works on the Arraylake catalog.
     An empty probe table got its partition spec in one metadata commit on
     2026-11-11. Then the probe was dropped.
   - The full kidney run measured each of these items, except the checksum of a `uint8` copy:
     - the download (991 s);
     - local reads;
     - the upload and the table commits, inside the 1,749.3 s of the transcripts step;
     - the polygon tables (16.0 s and 14.8 s);
     - the matrix copy (905.3 s);
     - the QC rasters;
     - levels above 0;
     - `verify.py --mode full` (2,246 s).
   - REPORT.md gives the counts.

   Estimate for one kidney sample at concurrency 32. It comes from the rates,
   not from a run. Levels above 0 add about one third, so it uses the
   factor 4/3.
   - 2D morphology: 4 x 1,848 x 4/3 = 9,856 tiles at 12.98 tiles/s: 759 s,
     about 13 minutes.
   - 3D stack: 1,848 x 4/3 = 2,464 positions at 0.436 positions/s: 5,651 s,
     about 94 minutes.
   - H&E: 1,008 x 4/3 = 1,344 tiles at 8.51 tiles/s: 158 s, about 3 minutes.
   - Two masks: 4,096 chunks at 33.4 chunks/s: 123 s, about 2 minutes.
   - Transcripts, Parquet files only: 1,186,813,834 rows at 548,733 rows/s:
     2,163 s, about 36 minutes. The 30 files total about 29.2 GB at the
     tile's 24.6 bytes per row. Their upload is not measured.
   - Sum: 759 + 5,651 + 158 + 123 + 2,163 = 8,854 s, about 2.5 hours. The
     unmeasured steps add to it. The 3D stack is 64 percent of the sum.
3. **Docs change.** The `AGENTS.md` section (call 11), and a `formats.md`
   note on JPEG 2000 in OME-TIFF: lossless 5/3 tiles decode once with
   `imagecodecs`; Booth cannot read the codec. Approval needed: the rules
   change.
4. **Kidney, full.** On a Coiled VM in us-west-2. The machine is an
   `m7g.xlarge` (4 vCPU, 16 GiB) with `--concurrency` 32. The plan author
   chose both from the second measurement. The owner confirms them before
   the full run. The peak memory of each part is under 1.3 GiB. The plane's
   CPU time, 48 s for 300 tiles, is far below 4 vCPU x its 23.1 s wall time
   at concurrency 32, which is 92 s. The stack decodes in one thread, so
   more vCPU do not shorten the largest step with the present code. The
   estimate is about 2.5 hours for the measured kinds. Step 2 gives the
   arithmetic. Several writer sessions on one branch need a merge or one
   branch per writer, not measured. The checksum on the R2 bucket is not
   measured. The disk must hold the 45.7 GB bundle and the 2.8 GB H&E, and
   one tile's Parquet file at a time, 2.1 GiB for the largest. Download the bundle and the H&E once, and verify their ETags. Read
   members in place. Write the tables, then the elements. "Stage two", item 4,
   sets the commit unit. Verify per `docs/ingest.md`: every image level against tifffile
   on a sample of tiles, every copied chunk by checksum, every table's row
   count against the source arrays, and the shim and zax reads. Write
   `REPORT.md`. Approval needed: the Coiled spend and about 100 GB of R2
   storage.
   - Result: the kidney run passed all 16 gates. The steps took 13, 14, 991,
     377, 6,829 and 2,246 s. REPORT.md gives the counts.
5. **Brain, colorectal, breast,** each as step 4, smallest first.
6. **Notebook** that opens one store cold, per `AGENTS.md`.

**Shared-package work.** Code that a second archive could use goes into
`minus80`, with tests, and reuses what is there first:

| Need | Reuse | Add |
|---|---|---|
| Arraylake repos as targets | `minus80.core.icechunk_io.arraylake_repository`, on `main` | nothing |
| OME-Zarr 0.5 image, label and multiscale metadata | `minus80.ome.image_attrs`, `label_attrs` | the SpatialData element attributes: `spatialdata_attrs` and `coordinateTransformations` |
| A native pyramid whose every level comes from the source | `minus80.core.multiscale`, which writes a virtual level 0 and computed levels | a path that writes decoded source levels as real chunks, with shards |
| JPEG 2000 tiles out of an OME-TIFF | tifffile's `page.decode`, which calls `imagecodecs` | `minus80.core.pyramid`: it decodes each tile once and writes it. It is in `core`, not `parsers.tiff`, because it writes real chunks and builds no references. |
| Zarr v2 chunks inside a stored zip, inside a stored zip | `minus80.parsers.zip`, which addresses members as chunks | a v2-to-v3 metadata translation and a byte copy of each chunk |
| Iceberg tables with the `iceberg_table` attribute and the `uint8` copy | `minus80.core.icechunk_io.create_repo`'s virtual chunk containers | a `minus80.core.tables` module: write Parquet with pyarrow, register with `add_files`, pin the snapshot in the attribute, declare the copy |

Expected runtime per sample has three parts. The download takes minutes at
an estimated 1 GB/s inside the region. The images, masks and transcript
Parquet files of the kidney take about 2.5 hours wall at concurrency 32 on
an `m7g.xlarge` in us-west-2. That figure comes from step 2's second
measurement. It is an estimate from rates. The upload of the Parquet files
and the table commits add to it. The transcript table write is one pass over
1 to 5 billion rows. A run that is well past its expected time stops.
The kidney run measured these. The download took 991 s for 48,518,883,775
bytes. The ingest took 6,829 s. The transcripts step took 1,749.3 s for
1,186,813,834 rows, with the upload and the commit.

Risks:

- **Access policy per repo** (call 12). Each sample's repo opens only when
  an org admin's Virtual Chunk Access Policy covers its container. Each
  sample's step needs that policy before the ingest starts.
- **Table region.** The fixture's table annotates `cell_labels`. The decision
  for `main` is `cell_boundaries`, joined to the labels and nuclei by
  `instance`. "Stage two" lists the change and its gate. zax reads the tables
  on the R2 endpoint: `LAYOUTS.md`, check (d).
- **The Python read path** rests on unreleased spatialdata patches and the
  virtual_omics shim. The fixture showed it: released SpatialData 0.8.0
  cannot open an Icechunk store, and PR #1107 with its patches and the shim
  opens every element. Readers outside our notebooks need those until
  spatialdata PR #1107 or the `parquet_fs_fallback` hook lands.
- **Booth and OME-Zarr 0.5.** Whether Booth serves a SpatialData image
  element as tiles is untested. It does not change the store. The elements declare
  `ome.version` `0.5-dev-spatialdata`. "Stage two", item 6, tests one element.
- **Node count.** About 15 images with 8 levels, 2 labels, 2 shapes and one
  table. The points have one part per source tile, 30 to 37 parts. That is a
  few hundred nodes per repo, far under the 500,000-node snapshot cache.

## Stage two

Stage two continues on the git branch `atera-ingest-plan`. It does the work
below, in this order. Then it ran the full kidney ingest to the Arraylake
branch `main`. The owner approved that write for the repo `atera-kidney`.
REPORT.md gives the results.
Each item gives the problem, the fix and the check.

1. **The table's region.**
   - Problem: the fixture's table annotates `cell_labels`. The decision in
     "The tables" is `cell_boundaries`.
   - Fix: `write_table` writes `cell_boundaries` to `obs/region` and to
     `write_table_keys`.
   - Check: a new verify gate. The set of `obs.instance` equals the set of
     `cell_boundaries.instance`. `cell_id` agrees for each instance. The
     `instances` gate checks only the mask.
2. **The 3D stack in shards.**
   - Problem: `write_page` writes one plane at a time. zarr's sharding codec
     reads, merges and writes the whole shard on each partial write. So each
     shard of 13 planes is written 13 times. Each write also leaves an orphan
     object in the Icechunk session. Two writers on planes of one shard
     conflict.
   - Fix: decode one `(row, col)` tile from every plane, then write its shard
     once. The other choice is no shards for the 3D stack. A measurement of
     both decides. Judgement call 7 records the decision.
   - Check: a test counts the writes of each shard. `check_page` compares
     sampled tiles on every plane.
3. **Transcript tiles in memory.**
   - Problem: `tile_table` builds a whole tile in memory. The largest kidney
     tile has about 90 million rows. An estimate puts its peak memory well
     above the 15 GiB of an `m7i.xlarge`. No run measured it.
   - Fix: build each tile in row slices of a few million rows. Write the
     slices as row groups with one `ParquetWriter`. Build `cell_id` with
     Arrow compute, not with NumPy string arrays.
   - Check: a test compares a sliced tile with a whole tile. A run on the VM
     measures the peak memory of the largest tile.
   - Result: `write_tile` on tile `3,1`, 90,630,655 rows, peaked at 1,299 MiB
     of 16 GiB on an `m7g.xlarge`. `LAYOUTS.md`, item (f), has the run.
4. **The commit unit and the resume key.**
   - Problem: `ElementWriter.unit` commits once per element.
     `images/morphology` and `images/morphology_3d` take hours each. A
     failure loses the whole element. `ElementWriter.has` tests only that the
     element's group exists. So with partial commits, a rerun skips
     unfinished levels.
   - Fix: commit per level or per plane. Write a completion attribute last,
     and resume on it. Several writers use the fork-and-merge method of
     `docs/ingest.md`, "Commit often, and merge parallel writers once".
   - Check: a test stops a run after one level and runs it again. The result
     equals one run without a stop.
5. **Shared code into `minus80`.**
   - Problem: `AGENTS.md` says that code a second archive could use moves
     into `minus80`. A Xenium ingest needs these parts of `ingest.py` and
     `bundle.py`.
   - Fix: move `Tables`, with its FileIO writer and `local_catalog`, into
     `minus80`. Also move the Zarr v2 read helper `minus80.core.zarr_copy.read_array`, `ElementWriter`
     and the read counter `ReadCounter`. Replace the hand-written v2 decoder with
     zarr's own reader if zarr can read a zip member.
   - Check: the moved tests pass under `tests/`, and the ingest imports the
     code from `minus80`.
   - Result: `TableWriter`, `ElementWriter`, `ReadCounter` with `counting_reader`,
     and `read_array` are in `minus80.core`.
6. **The OME version of the raster elements.**
   - Problem: the image and label elements declare `ome.version`
     `0.5-dev-spatialdata`. SpatialData requires that string. `AGENTS.md`
     asks for OME-Zarr 0.5 metadata. A 0.5 reader that checks the string can
     refuse the element.
   - Fix: record the version as a judgement call here and in `LAYOUTS.md`.
   - Check: open one element with `ome-zarr-models` and with Booth before
     the full ingest. Record both results.
   - Result: `ome-zarr-models` 1.8.1 rejects both elements on the version
     string only. SpatialData 0.8.0 parses their transforms. Booth is not
     checked. Judgement call 14 and "The OME version check" in `LAYOUTS.md`
     give the text.
7. **The H&E image.**
   - Problem: the kidney H&E has Deflate tiles. The brain H&E has
     uncompressed planar tiles. The Zarr core has no codec for the zlib
     stream of a Deflate tile. It has one for uncompressed planar tiles. The
     "Native ingests" rule says to copy a chunk's bytes unchanged when one
     can.
     `write_page` always decodes. Its default chunk is `(1024, 1024)`,
     whatever the H&E tile size is.
   - Fix: the ingest decodes the H&E of every sample, one rule for all four
     samples. Judgement call 5 records the reason. The ingest passes the
     page's tile size as the chunk, as `measure.py` does.
   - Check: the `images` gate on every H&E level, against tifffile.
8. **Concurrent chunk writes and a second measurement.**
   - Problem: each written chunk took 0.55 s on the VM. The cause is not
     measured. Step 2 measured no concurrent chunk writes.
   - Fix: write chunks concurrently inside one process. Measure again on a
     Coiled VM in us-west-2, with the machine token, not a personal one.
   - Check: the measurement gives the time per chunk and the peak memory.
     It sets the machine size for step 4.
   - Result: done on 2026-11-11 on an `m7g.xlarge` with the machine token.
     Concurrency 32 gives 8.0 times the rate of concurrency 1 for the plane,
     17.8 for the mask, 7.2 for the H&E and 1.5 for the stack. The peak
     memory of each part is under 1.3 GiB. Step 4 states the machine size
     and the concurrency. `LAYOUTS.md`, item (f), has the numbers.
9. **The full kidney ingest** to the Arraylake branch `main`, as step 4.
   It keeps the fixture's two tables, `transcripts` and `nucleus_boundaries`,
   and their UUIDs. Item 13 gives how `--replace-tables` replaces their rows.
   It adds `cell_boundaries` as a new table. The branch `fixture` keeps its
   pinned snapshots. The ingest never overwrites their files.
   - Result: the run ended on 2026-11-12 with all 16 gates passed.
     REPORT.md gives the counts and the times.
10. **Deferred minor items.** These smaller items may wait:
    - `minus80.parsers.zip.nested`: `Window.seek` and its tests, and the zip
      that stays open.
    - `minus80.core.zarr_copy`: the refusals and their tests.
    - `minus80.core.pyramid`: `write_page` skips a missing tile silently;
      `_check_shape` checks only `y` and `x`. A core test and an ingest test check a planar page.
    - `minus80.ome.spatialdata`: checks on the input of a transform.
    - `minus80.core.tables`: partial references after a failed write, and
      `require_table` on a table with no snapshot.
    - The ingest's tests: the conftest, and a close on `Bundle`.
    - `bundle.py` and `polygons.py`: tests of `read_array`, and the loop
      over polygons.
    - `ingest.py` and `verify.py`: `ReadCounter`, the lambdas, and repeated
      checks.
    - Prose fixes in this file and in `docs/upstream.md`.
11. **Small fixes.**
    - `--repo` and `--catalog` both use `arraylake://<org>` for the org.
    - State which virtual chunk container the full ingest uses: the table's
      `data/` prefix, or the namespace's storage location.
    - `require_table` creates a partitioned table and its partition spec in one staged create.
    - Run the full ingest with a service token, or a token limited to the
      repo, not a personal token. `REPORT.md` records which.
    - `check_bundle_size` takes the first source object as the bundle. Match
      the object by its `_outs.zip` suffix.
    - `gate_copies` reads each file and array whole. Hash them in blocks.
    - `verify.chunk_members` does not handle the `/` chunk separator that
      `zarr_copy` handles.
    - Result: `gate_copies` hashes in blocks, and `verify.chunk_members` handles the `/` separator.
      `--repo` and `--catalog` use the one `arraylake://<org>` form.
      `check_bundle_size` selects the bundle's row by its `_outs.zip` name.
12. **The dry run of the full mode.**
    - Problem: the full mode needs a check on a complete store before the
      cloud run.
    - Fix: none. The run found no fault in `ingest.py` or `verify.py`.
    - Check: on 2026-11-11, on a laptop, a synthetic bundle, a local repo and
      a local catalog. All 13 gates that ran passed. `masks` and `instances`
      need the real bundle. Two runs that SIGINT stopped, one in the
      transcript tiles and one in `images/morphology`, resumed to the store
      of an unstopped run. The arrays are equal. Only the ids of the six
      tables differ. This measures no speed. `LAYOUTS.md`, "The dry run of
      the full mode", has the commands and the results.
13. **Replace the held tables in place.**
    - Problem: the full ingest writes `transcripts` and `nucleus_boundaries`,
      which the `fixture` branch pins. The held `nucleus_boundaries` has
      another schema. The held `transcripts` tile `0,0` starts at `instance` 0,
      and on `main` it starts at 1. The ingest stops on both by default.
    - Fix: `ingest.py --mode full --replace-tables transcripts,nucleus_boundaries`.
      `require_table` evolves the schema in one commit with `update_schema`.
      `write_transcripts` keeps a tile that passes its checks.
      It replaces a failing tile in one overwrite snapshot.
      It removes a tile outside the plan in a snapshot first.
      `partition_bounds` reads the schema of the snapshot it is given.
    - Check: tests on a local catalog build the fixture's two tables and
      elements by hand. They run the replace. Then they run the gates
      `copies`, `attrs` and `field_ids` on the fixture. All pass. A scan of each earlier
      snapshot returns the earlier rows and columns. The earlier files keep
      their bytes and their times. A stop at each point, then a rerun, ends
      with the table of an unstopped run.
14. **Levels above 0.**
    - Fact: `level_scale` gives each level a scale that sets its pixel size.
      It gives no translation.
    - The owner checks this call.

## Before the next sample

Open work, as present facts.

- **Morphology members.** The ingest finds the morphology members by fixed
  names (`bundle.py`). Another sample with other stain names stops at the
  image step, after the tables. The names must be resolved by pattern and
  checked when the bundle opens.
- **Root attributes.** The ingest builds the root attributes in its last step.
  A bundle with other metric or `experiment.spatial` keys stops there, after
  every image. The attributes must be built when the bundle opens.
- **Transforms.** No gate checks an element's transform: the image and label
  scales, the stack's z step, the H&E affine against the alignment CSV. A
  `transforms` gate is not written.
- **Branch.** The full ingest writes branch `main` before the verification.
  `docs/ingest.md` asks for a branch first for a new source. The launcher
  accepts a branch `trial-<name>`. The owner decides for each sample.
- **Facts of the other samples.** The other samples use analysis version 1.1.1.
  The survey recorded them (`LAYOUTS.md`, "The four bundles"). The member names,
  the QC labels and the metric keys equal the kidney's. The H&E is planar with 9
  levels in all three. The stack has 15 planes in the brain and 13 in the others.
  The transcript grids have 35, 33 and 37 tiles. Each bundle check compares a
  bundle with its record.
- **Approvals.** Each sample needs its repo, its namespace, its access policy,
  the Coiled spend and the storage. The owner approves each one.
- **The 3D stack.** The stack decodes its planes in one thread. It took
  3,257.4 s of the kidney ingest's 6,829 s.
- **The root attribute `description`.** It does not mention
  `images/morphology_3d`, `images/he_image`, the QC rasters or
  `iceberg_tables`. A change needs one more commit on `main` of a written
  sample.
- **Verification against the ingest's code.** `verify.py` checks three things
  against the ingest's own code: the chunk-key mapping, the copied arrays'
  metadata, the QC rasters' decode.

## Stage three

Stage three prepares the ingest of the brain, colorectal and breast samples.
It closes each item of "Before the next sample". It does not ingest a sample.
Each sample's run is a later step with its own approval.

### Intent

The kidney run proved the flow on one bundle of analysis version 1.0.4.
The three other bundles have version 1.1.1 and are larger.
A difference between the versions must stop a run at its start, not after hours.
A reader of `main` must never see a store that the verification did not pass.

### Goals

1. The facts of each 1.1.1 bundle are on record before any write.
2. A bundle that differs from the record stops the run before the download.
3. A fault inside a bundle stops the ingest when the bundle opens, before the first write.
4. The verification checks where each element is in `global`.
5. `main` of a sample's repo moves only to a snapshot that passed every gate.
6. The 3D stack takes less time.
7. The store's own description lists each kind of element it holds.

### Not goals

- A change of the layout of `main`. The kidney's layout stays.
- A change of the kidney's data. Its `main` gets one commit, for the root attribute `description` only.
- The overview scan. It stays out.
- A second measurement run. Each sample's own run gives its times.
- A move of ingest code into `minus80` beyond what an item below needs.

### Decisions of the owner

- **The survey runs on Coiled.** It does not run on a laptop.
- **A trial branch first.** Each sample's run writes a branch `trial-<name>`.
  `main` moves to that snapshot after every gate passes.
- **The setup on Arraylake.** The plan's author creates each repo, each tables namespace and each access policy with the Python client.
  The owner approves each one first.
- **The order.** Brain, then colorectal, then breast: the smallest bundle first.

### Calls of the plan's author

The owner checks each of these.

- The remote script moves `main` by itself when every gate passed. It does not wait for a person.
- A failed gate leaves `main` at the repo's first commit and keeps the trial branch for a look.
- The work continues on the git branch `atera-ingest-plan`.
- The machine stays `m7g.xlarge` with `--concurrency 32`. The launcher sets the disk from the sample's download size.
- The kidney's `description` commit runs on a VM. The step reads the bundle by range request and writes the root attributes only.
- The QC rasters' decode has no second decoder. `REPORT.md` keeps that limit.
- The survey writes one record file for each sample, `samples/<sample>.json`. The bundle check compares a bundle with that file.
- The bundle check runs before the download, by range request. It runs again on the local file before the ingest.
- A rerun on the same trial branch resumes. A fix that changes written arrays uses another trial branch name.
  A fix that changes a table's rows uses `--replace-tables`. No run deletes a branch, a table or an object.
- `main` moves only forward: its snapshot must be an ancestor of the trial snapshot.
- The kidney's `description` commit also goes through a trial branch and the move of `main`. It is the first use of that move.
- The plan's author runs the setup script with the owner's login. The machine token has no right to set a policy.

### The work

**1. The survey.** A script `survey.py` reads metadata from the four bundles and prints one record for each sample.
It runs on one VM through the launcher's script form. It reads by range request and downloads no bundle.
It writes nothing to Arraylake.
It reads metadata and the small index arrays: no pixel, no transcript column, no matrix chunk.
It prints the bytes that it read from each bundle.
The record holds:

- the member names of the bundle, with sizes;
- the keys of `experiment.spatial` and the analysis version;
- the name of the metrics member and its keys;
- for each morphology file and the H&E: levels, shapes, tile size, compression, planar or interleaved, the OME pixel sizes;
- the plane count of the 3D stack;
- the transcript grid: the tile keys, the rows of each tile, the chunk rows of each column;
- the polygon sets: rows, unique cells, the method codes;
- the `cell_summary` columns;
- the QC labels, units, bin counts and data types of the analysis summary;
- the columns of the two differential expression files;
- the alignment matrix.

The kidney is in the survey as a control. Its record must equal what `LAYOUTS.md` holds.
The survey writes each record as `samples/<sample>.json`. The file is committed.
`LAYOUTS.md` gets a table of the differences between the samples. A difference from the kidney is a finding with its number.

Result: the survey ran on 2026-11-12 on one `m7g.xlarge` in us-west-2 and wrote the four records.
It read 146,999,959, 103,065,174, 152,937,056 and 169,825,963 bytes in 1,852, 1,253, 1,733 and 1,927 requests.
These are the kidney, brain, colorectal and breast. The kidney's reads total 3.5 times the 42 MB of the plan's estimate.
Each kidney fact of `LAYOUTS.md` and `REPORT.md` equals the record.
The colorectal and breast records hold an error entry at `qc.rasters`: the reader did not know `uint32`.
`LAYOUTS.md` has the table in "The four bundles".

**2. Members by pattern.** The ingest finds the morphology files by `morphology_2d/ch*.ome.tif` and `morphology_3d/ch*.ome.tif`.
It finds the metrics member and the report by one pattern each. Each pattern must match the expected count, or the open of the bundle fails.

**3. A check when the bundle opens.** One function reads every fact that a later step needs.
It builds the root attributes and resolves each member.
It reads the QC labels and the transcript grid, and it opens each TIFF header.
It compares each fact with the sample's record file. A difference is one line that gives the fact and both values.
The ingest calls it before the first write. The verification calls it too.
The remote script runs it twice: by range request before the download, and on the local file before the ingest.

**4. A `transforms` gate.** It compares each element's transform with the source:

- each image and label level's scale, against the `experiment.spatial` pixel size and the level shapes;
- the stack's z step, against the OME-XML;
- the H&E affine and the `alignment` attribute, against the CSV, which the gate parses itself;
- each QC raster's scale and translation, against the report's bins and origin;
- the result of the corners of each level through each stored transform: four corners of a plane, eight of the stack.

The gate computes each expected value from the source and not from the ingest's functions.
A stored number must equal its source number within a relative error of 1e-9.
The alignment CSV is a 3 by 3 matrix in `(x, y)` order. It maps H&E pixels to morphology pixels, as `LAYOUTS.md` records.

**5. A trial branch, then `main`.** The launcher's ingest form writes `trial-<name>`.
After `=== all steps passed` for the verification, a last step moves `main` to the trial branch's snapshot.
The step refuses when the snapshot of `main` is not an ancestor of the trial snapshot.
So `main` moves only forward, and the table pins of the verified snapshot stay as written.
A trial branch has the name `trial-<date>`, or `trial-<date>-<n>` for a later try.
A failed gate, or a stop in the ingest, keeps the trial branch. A rerun on that branch resumes.
Result: on 2026-11-12 the kidney's `description` run moved `main` on Arraylake with the machine token.
`main` went from `KB0DWEXDHY8GSRE29GQG` to `DCHK9523X4W5TSC4V5X0`, the snapshot that the verification read.
The verification of a later run can read either branch.

**6. Threads for the stack.** `write_stack` decodes the planes of one position in a thread pool.
The bound on pending bytes stays. A test compares the result with the one-thread result.

**7. The `description`.** The root attribute names `images/morphology_3d`, `images/he_image`, the QC rasters and `iceberg_tables`.
The kidney gets one commit with it, on a trial branch. The gates `complete`, `attrs` and `source` run on that branch, then `main` moves.
The `complete` gate checks that the text names each element kind that the store holds.
Result: on 2026-11-12 one run wrote the kidney's root with `--root-keys description` on `trial-20261112-description`.
The ingest printed `root attributes: changes description`. The gates `complete`, `attrs` and `source` passed.
`main` moved forward by that one commit. The six tables kept their snapshots, and `fixture` stayed as it was.

**8. Checks with their own reference.** The chunk-key mapping moves to one function in `minus80.core.zarr_copy`.
The matrix and label gates also compare decoded values of sampled chunks with the source's decode.
The gate for copied arrays' metadata reads each source `.zarray` field itself.

**9. The setup on Arraylake.** A script creates, for one sample: the repo, the tables namespace, and the access policy for the namespace's prefix.
It has a dry run. It prints no bucket name and no prefix.
It reads the namespace's location from the catalog at run time. No file holds it.
Each piece that exists and is as wanted is skipped. A piece that exists and differs stops the script.

### Facts that change stage three

- The density map of the colorectal and breast reports is `uint32`. The QC reader accepts `uint32`.
- The breast has 9 morphology levels in the four 2D files and in the stack. The other samples have 8.
- The largest tile is in the colorectal sample: 334,725,210 rows. The kidney's largest has 90,630,655 rows. The ratio is 3.7.
- The transcript rows are 1,640,508,930, 3,855,378,414 and 4,565,138,232 for the brain, colorectal and breast.
- Estimate of the transcript tables. The kidney's tile `3,1` measured 24.6 bytes per row of Parquet.
  That is 2,230,658,968 bytes for 90,630,655 rows.
  Then 1,640,508,930 x 24.6 is 40.4e9, 3,855,378,414 x 24.6 is 94.8e9, and 4,565,138,232 x 24.6 is 112.3e9.
  The tables come to about 40 GB, 95 GB and 112 GB.
- Estimate of the scratch file of the largest tile. 334,725,210 x 24.6 is 8.2e9 bytes, or 7.7 GiB.
  The launcher allows 10 GiB for this file.

### The run of one sample

Each run needs the owner's approval of the repo, the spend and the storage.

1. The setup script, after approval.
2. The launcher's ingest form, on one VM: environment, preflight, space check, download, file check, bundle check, ingest to `trial-<name>`, verification, move of `main`.
3. A check that the cluster stopped.
4. `REPORT.md` gets a section for the sample: the counts, the times, the findings.
5. The reader project gets the `main` snapshot and the table pins.

### Sizes

| Sample | Bundle | H&E | Download |
|---|---|---|---|
| brain | 48,858,124,574 | 6,519,718,813 | 55.4 GB |
| colorectal | 75,341,543,536 | 5,067,976,962 | 80.4 GB |
| breast | 89,305,623,909 | 7,318,928,225 | 96.6 GB |

The kidney's download took 991 s for 48.5 GB, and its ingest 6,829 s.
The survey gives each sample's transcript rows, pixels and planes.
The estimate of a run scales the kidney's times: the download and the file check by bytes, the transcripts by rows, each image by its pixels and planes.
The estimate assumes no gain from the stack's threads. The time limit is twice the estimate.

### Left to the builder

- The names and signatures of functions.
- The form of the survey's record, if `LAYOUTS.md` can hold it as tables.
- The thread count of the stack's decode.
- The test data for a 1.1.1 layout. It has a planar H&E with 9 levels and a stack of 15 planes.
  It has 9 morphology levels and a `uint32` density map.
