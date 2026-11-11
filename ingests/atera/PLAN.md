# 10x Atera WTA: ingest plan

## Summary

This ingest copies four 10x Atera whole-transcriptome samples into Arraylake as
native Icechunk repos, one repo per sample, in the `earthmover-biology` org. It
is the first native ingest in minus-80. Every other ingest references the
archive's bytes in place. This one copies them, so that readers hit our bucket
and never 10x's public bucket.

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
| 2 | The table's instance key | Decided: the integer label in the masks, which is the row position plus one. `obs` keeps `barcode` and `cell_id`. |
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

- Native copy, not virtual references, so that readers never hit `s3://10x.files`.
- Four samples, smallest bundle first: kidney, brain, colorectal, breast.
- Org `earthmover-biology`, default bucket `earthmover-managed` (Cloudflare R2).
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
| `source` | the four objects: URL, size, ETag and last-modified date of each |
| `documentation` | the dataset page URL and the Atera Onboard Analysis output documentation URL |
| `license` | `"Creative Commons Attribution 4.0 International (CC BY 4.0)"` and its URL |
| `citation` | `text`: the rendered citation in 10x's format, as `LAYOUTS.md` gives it; `dataset_title`; `product`: `"Atera In Situ"`; `software`: `"10x Genomics Atera Onboard Analysis v1.0.4"`, the version as the page states it; `software_version_full`: `analysis_sw_version`, such as `atera-1.0.4.1`; `instrument_software`: `instrument_sw_version`; `publisher`: `"10x Genomics"`; `published`: the dataset page's date; `url`: the dataset page; `doi`: `null`, with a note that 10x lists none |
| `reading` | which readers open which elements, and what the shim is for |
| `sample`, `run`, `panel`, `metrics` | `experiment.spatial` and `metrics_summary.csv`, typed |
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
| `transcripts.zarr.zip` | Zarr v2 grid of 28 to 32 tiles, six column arrays each | 12.8 GB | `points/transcripts`, as an Iceberg table and a `uint8` copy |
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
images/qc_transcript_density        (c, y, x) uint16, one channel, 20 µm bins, from the analysis summary's data bag
images/qc_mean_q_score              same grid, float32
images/qc_mean_q_score_80um         80 µm bins, float32
images/qc_negative_control_codewords
images/qc_negative_control_probes
images/qc_genomic_controls
labels/cell_labels                  (y, x) uint32, chunks copied
labels/nucleus_labels               (y, x) uint32, chunks copied
shapes/cell_boundaries              iceberg_table + shapes.parquet as a uint8 array
shapes/nucleus_boundaries           iceberg_table + shapes.parquet as a uint8 array
shapes/region_outline               the ROI outline from the overview scan, one polygon (call 6)
points/transcripts                  iceberg_table + points.parquet/part.<i>.parquet, one part per source tile
tables/table                        AnnData: X CSR int32, obs, var, obsm; region cell_labels
```

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
- The H&E maps into `global` by the alignment CSV's affine composed with its
  own pixel scale. The matrix stays in the element's attributes as published.
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

Polygons, one row per cell or nucleus, from `polygon_sets/<i>`:

| Column | Type | From |
|---|---|---|
| `instance` | int64 | the mask label, the index column |
| `cell_index` | int64 | `cell_index`, as published, widened from uint32 |
| `cell_id` | string | the Xenium form |
| `method` | int64 | `method`, widened from uint32, with the method names in the table properties |
| `geometry` | binary, WKB polygon | `vertices[:num_vertices]`, in micrometres |
| `bbox` | struct of four float64 | the polygon's bounds |

Every table carries the `geo` property with GeoParquet 1.1 metadata. Every
Parquet file carries the same `geo` key and the pandas index metadata in its
footer. That is possible because the ingest writes the files with pyarrow and
registers them with pyiceberg `add_files`. zax reads the footer, and the shim
reads the index.

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
never expires, compacts or rewrites these tables. Each table's properties name
the repo that references its files.

The `uint8` copies are never real chunks. If Arraylake's catalog refuses
`add_files`, the tables take pyiceberg's own `append`, whose files lack the
footer metadata, and the copies reference those files. The shim then takes
the `geo` metadata from the table property and the index column from the
`iceberg_table` attribute, which is what zax reads already. If the repo
cannot reference files in the R2 bucket at all (call 12), the copies are
dropped, points and shapes are Iceberg-only, and the shim scans the table.
The fixture on Arraylake used neither fallback: the catalog accepted
`add_files`, and the repo references the files through its container.

Shapes tables carry an `instance` column that equals the mask label. It is the
`index_column`. The source's `cell_index` stays as a column. The table element
uses the same label as its instance key. So a join from the table to the
shapes or the labels never shifts by one.

### Layouts considered and rejected

- **Virtual references into `s3://10x.files`.** Rejected: the stated goal is
  to take load off 10x's bucket. The virtual repos in `earthmover-demos` stay
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
- **The HTML reports as `file:` arrays.** Rejected: the convention is
  unpublished. Their data bag reduces to attributes and the QC rasters.
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
  transcripts store has 28 tiles (kidney) and 32 (brain) of six arrays each.
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
  rasters are numeric, not pictures: base64 zip streams typed `uint16` for
  the density and control maps and `float32` for the quality maps, one
  channel each, with their grid, origin, unit and the full-resolution image
  size stated beside them.
- **Metadata files.** `experiment.spatial`, the alignment CSVs,
  `metrics_summary.csv`, `panel_config.json` and both FOV location JSONs
  were read. The alignment is a 3 by 3 affine with a scale of 1.289 and a
  90-degree rotation.
- **Arraylake.** The org's bucket configurations, repos and the one Iceberg
  namespace were listed. The org's three bio repos were walked for codecs
  and chunking; the cellpainting repo fails to open without `imagecodecs`.

Tested in the fixture step, on 2026-11-10 and 2026-11-11, from a laptop:
decoding a JPEG 2000 tile with `imagecodecs` against tifffile; writing a
table through `add_files` on Arraylake's catalog; virtual references from an
Arraylake repo into the R2 bucket; the shim reading the `uint8` copies on
Icechunk. `LAYOUTS.md`, "Fixture findings", gives the results.

Not tested: zax reading an Iceberg table on an S3-compatible endpoint, which
the zax-spatialdata project checks; decode and write throughput on a Coiled
VM, which step 2 measures.

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
   `barcode`.
3. **`float16` quality scores.** Parquet's FLOAT16 type is young, and zax's
   Arrow support for it is unverified. Recommended: `float32` in the tables,
   recorded in the table properties and in `LAYOUTS.md`.
4. **Micrometres as the global frame.** `spatialdata_io.xenium` puts images
   under `Scale(pixel_size)` and leaves points and shapes in micrometres.
   Recommended: the same, so the store reads like a Xenium one.
5. **The H&E as a transform.** Resampling would decode and rewrite an
   estimated 3 to 7 GB per sample and lose the original grid. Recommended:
   keep the pixels, place with the affine, keep the matrix in attributes.
6. **The overview scan.** Its FOV boxes are in scan pixels; the morphology FOV
   boxes are in micrometres; both JSONs list the same FOV ids. A least-squares
   affine over 78 boxes places the scan. Recommended: include it, and the ROI
   outline with it, when the residual is under one morphology pixel; else
   leave both out and say why.
7. **The 3D stack.** 19 to 23 GB per sample as JPEG 2000, about twice that as
   zstd. It is the only 3D data, and the virtual kidney store had it.
   Recommended: include it.
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
    Virtual Chunk Access Policy on `earthmover-managed` for the namespace's
    location. The repo's container is that location, so it sits at or under
    the policy's location. Without the policy, Arraylake refuses to open the
    repo, with HTTP 400. With it, the fixture's `copies` gate passed: each
    copy's bytes equal its Parquet file. The shim read both copies through
    Arraylake with no credentials of its own. Each sample's repo needs the
    same policy for its own namespace.

## Steps, scope and risk

Each step needs approval before it writes to a shared resource.

1. **Fixture, kidney, branch `fixture`.** Create the repo `atera-kidney` and the
   namespace `atera-kidney-tables`. Write level 6 of `images/morphology`, the
   two masks' lowest pyramid level, one transcript tile as a table and a
   `uint8` copy, the nucleus polygons as a table and a copy, and the AnnData
   `obs` and `var`. Then: open it with the virtual_omics shim, in that
   project's `spatial` environment, and with stock SpatialData 0.8; send its
   coordinates to the zax-spatialdata project for its Iceberg source and
   tests; check the R2 virtual references; check `add_files` on the
   catalog; decode one JPEG 2000 tile against tifffile. Pass criteria: the
   shim opens every element; stock SpatialData opens the images, the labels
   and the table, and reports the points and shapes as absent, which is the
   expected limit. Approval needed: creating one repo and one namespace in
   `earthmover-biology`.

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
2. **Throughput on Coiled.** On a Coiled VM in us-west-2, decode and write
   one full-resolution morphology plane, and copy one label mask. Write to
   a scratch branch, not `fixture` or `main`. Time each. These two numbers give the expected runtime for
   step 4. Approval needed: the Coiled spend.
3. **Docs change.** The `AGENTS.md` section (call 11), and a `formats.md`
   note on JPEG 2000 in OME-TIFF: lossless 5/3 tiles decode once with
   `imagecodecs`; Booth cannot read the codec. Approval needed: the rules
   change.
4. **Kidney, full.** On a Coiled VM in us-west-2 with 32 cores and 200 GB of
   disk. Download the bundle and the H&E once, and verify their ETags. Read
   members in place. Write the tables, then the elements, with one commit per
   element. Verify per `docs/ingest.md`: every image level against tifffile
   on a sample of tiles, every copied chunk by checksum, every table's row
   count against the source arrays, and the shim and zax reads. Write
   `REPORT.md`. Approval needed: the Coiled spend and about 100 GB of R2
   storage.
5. **Brain, colorectal, breast,** each as step 4, smallest first.
6. **Notebook** that opens one store cold, per `AGENTS.md`.

**Shared-package work.** Code that a second archive could use goes into
`minus80`, with tests, and reuses what is there first:

| Need | Reuse | Add |
|---|---|---|
| Arraylake repos as targets | `minus80.core.icechunk_io`, with the `cellpainting-gallery` branch's `arraylake://` support | nothing |
| OME-Zarr 0.5 image, label and multiscale metadata | `minus80.ome.image_attrs`, `label_attrs` | the SpatialData element attributes: `spatialdata_attrs` and `coordinateTransformations` |
| A native pyramid whose every level comes from the source | `minus80.core.multiscale`, which writes a virtual level 0 and computed levels | a path that writes decoded source levels as real chunks, with shards |
| JPEG 2000 tiles out of an OME-TIFF | `minus80.parsers.tiff`, which indexes tiles and pages | a decode of one tile through `imagecodecs`, verified against tifffile |
| Zarr v2 chunks inside a stored zip, inside a stored zip | `minus80.parsers.zip`, which addresses members as chunks | a v2-to-v3 metadata translation and a byte copy of each chunk |
| Iceberg tables with the `iceberg_table` attribute and the `uint8` copy | `minus80.core.icechunk_io.create_repo`'s virtual chunk containers | a `minus80.core.tables` module: write Parquet with pyarrow, register with `add_files`, pin the snapshot in the attribute, declare the copy |

Expected runtime per sample has three parts. The download takes minutes at
an estimated 1 GB/s inside the region. The JPEG 2000 decode of 17 to 19
planes is not measured; step 2 measures it. The fixture's laptop times do
not predict it. The transcript table write is one pass over 1 to 5 billion
rows. A run that is well past its expected time stops.

Risks:

- **Access policy per repo** (call 12). Each sample's repo opens only when
  an org admin's Virtual Chunk Access Policy covers its container. Each
  sample's step needs that policy before the ingest starts.
- **zax on an S3-compatible endpoint.** zax's Iceberg storage is written for
  S3 and GCS. R2 speaks S3 with an endpoint URL. The zax-spatialdata
  project answers this from the fixture's coordinates.
- **The Python read path** rests on unreleased spatialdata patches and the
  virtual_omics shim. The fixture showed it: released SpatialData 0.8.0
  cannot open an Icechunk store, and PR #1107 with its patches and the shim
  opens every element. Readers outside our notebooks need those until
  spatialdata PR #1107 or the `parquet_fs_fallback` hook lands.
- **Shared-package additions** this ingest needs from the unmerged
  `cellpainting-gallery` branch, `arraylake://` locations in
  `minus80.core.icechunk_io`, land before this ingest starts, or it rebases
  onto that branch.
- **Booth and OME-Zarr 0.5.** Whether Booth serves a SpatialData image
  element as tiles is untested. It does not change the store.
- **Node count.** About 15 images with 8 levels, 2 labels, 5 points parts,
  2 shapes and one table: a few hundred nodes per repo, far under the
  500,000-node snapshot cache.
