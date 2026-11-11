# 10x Atera WTA: how the bundles map onto the stores

This file grows with the ingest. The first section holds what the 10x dataset
pages state, copied by hand on 2026-11-10 from saved copies of the pages,
because the pages block automated reads. A person checks each row against the
page before the ingest writes it.

## Attribution

10x's citation format for a dataset, from its citation guidelines page:

> *Dataset title*, *Product* Dataset by *Software* v*x.y.z*, 10x Genomics,
> (*YYYY, Month D*).

The pages state the software version with three parts. `experiment.spatial`
states four, in `analysis_sw_version`. The citation uses the page's form, and
the root attributes keep both.

| Repo | Dataset title | Page | Published | Software |
|---|---|---|---|---|
| `atera-kidney` | Atera In Situ Gene Expression, FFPE Human Kidney | https://www.10xgenomics.com/datasets/atera-wta-ffpe-human-nd-kidney | 2026-10-15 | Atera Onboard Analysis v1.0.4 (`atera-1.0.4.1`) |
| `atera-brain` | Atera In Situ Gene Expression, Fresh Frozen Human Brain | https://www.10xgenomics.com/datasets/atera-wta-ff-human-nd-brain | 2026-11-03 | Atera Onboard Analysis v1.1.1 (`atera-1.1.1.0`) |
| `atera-colorectal` | Atera In Situ Gene Expression, Fresh Frozen Human Colorectal Cancer | https://www.10xgenomics.com/datasets/atera-wta-ff-human-crc | 2026-11-05 | Atera Onboard Analysis v1.1.1 (`atera-1.1.1.0`) |
| `atera-breast` | Atera In Situ Gene Expression, Fresh Frozen Human Breast Cancer | https://www.10xgenomics.com/datasets/atera-wta-ff-human-breast-cancer | 2026-11-05 | Atera Onboard Analysis v1.1.1 (`atera-1.1.1.0`) |

The rendered citations:

- Atera In Situ Gene Expression, FFPE Human Kidney, Atera In Situ Dataset by
  Atera Onboard Analysis v1.0.4, 10x Genomics, (2026, October 15).
- Atera In Situ Gene Expression, Fresh Frozen Human Brain, Atera In Situ
  Dataset by Atera Onboard Analysis v1.1.1, 10x Genomics, (2026, November 3).
- Atera In Situ Gene Expression, Fresh Frozen Human Colorectal Cancer, Atera
  In Situ Dataset by Atera Onboard Analysis v1.1.1, 10x Genomics,
  (2026, November 5).
- Atera In Situ Gene Expression, Fresh Frozen Human Breast Cancer, Atera In
  Situ Dataset by Atera Onboard Analysis v1.1.1, 10x Genomics,
  (2026, November 5).

Every page states: "This dataset is licensed under the Creative Commons
Attribution 4.0 International (CC BY 4.0) license", with a link to 10x's
citation guidelines. The guidelines list no publication for Atera, so the
citation has no DOI. The pages' publication dates for the colorectal and
breast samples, 2026-11-05, are one day after their objects' last-modified
dates, 2026-11-04. The citation uses the page's date.

### What each page states about the sample

| Repo | Biomaterials | Preparation | Instrument and analysis |
|---|---|---|---|
| `atera-kidney` | A non-diseased human kidney FFPE tissue block from Donor Network West. | Atera Gene Expression - FFPE Tissue Preparation Handbook (CG001671); Atera Gene Expression User Guide (CG001672); Post-Atera H&E Staining Demonstrated Protocol (CG001722). | Atera Instrument User Guide (CG001673); Atera Onboard Analysis v1.0.4. |
| `atera-brain` | Fresh frozen, non-diseased human brain tissue (hippocampus region) from ProteoGenex, pre-embedded in OCT. | Atera Gene Expression - Fresh Frozen Tissue Preparation Handbook (CG001670); CG001672; CG001722. | CG001673; Atera Onboard Analysis v1.1.1. |
| `atera-colorectal` | Fresh frozen human colon sample (colorectal adenocarcinoma) from Discovery Life Sciences, snap-frozen by the vendor and embedded in OCT in-house. | CG001670; CG001672; CG001722. | CG001673; Atera Onboard Analysis v1.1.1. |
| `atera-breast` | Fresh frozen human breast sample (infiltrating ductal carcinoma) from ProteoGenex, pre-embedded in OCT. | CG001670; CG001672; CG001722. | CG001673; Atera Onboard Analysis v1.1.1. |

The pages' metric tables agree with `metrics_summary.csv` in each bundle:
number of cells detected, median transcripts and genes per cell, total high
quality decoded transcripts, and region area. The page labels the species
Human, the anatomical entity (Kidney, Brain, Colon, Breast), the preservation
method, the disease state (Healthy, Healthy, Colorectal cancer, Breast cancer)
and "Cells" for cells or nuclei. The root attributes carry these under
`sample`.

### The objects

All under `https://s3-us-west-2.amazonaws.com/10x.files/samples/atera/<version>/<sample>/`,
also at the matching key on `https://cf.10xgenomics.com/`. Sizes and ETags
from HEAD requests on 2026-11-10. The ingest reads them as `s3://10x.files/...`, unsigned:
obstore cannot parse the path-style HTTPS URL, and refuses the CDN's answer to a range read of a bundle.

| Repo | Object | Size | ETag | Last modified |
|---|---|---|---|---|
| `atera-kidney` | `Atera_v1_WTA_Kidney_non_diseased_FFPE_outs.zip` | 45,720,445,144 | `8c1c07343dbe1b946f5f90a21130782f-5451` | 2026-10-15 |
| | `Atera_v1_WTA_Kidney_non_diseased_FFPE_he_image.ome.tif` | 2,798,438,508 | `b87fddd2e5d93223bb40758c6bfb1366-334` | 2026-10-15 |
| | `Atera_v1_WTA_Kidney_non_diseased_FFPE_he_imagealignment.csv` | 123 | `db6cd13df295292000e42656df8bbc9b` | 2026-10-15 |
| | `Atera_v1_WTA_Kidney_non_diseased_FFPE_experiment.spatial` | 2,896 | `ed70b43593bb552e16bf35fbc278e222` | 2026-10-15 |
| `atera-brain` | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_outs.zip` | 48,858,124,574 | `359ca38cb4670d0549d251af698f070a-5825` | 2026-11-03 |
| | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_he_image.ome.tif` | 6,519,718,813 | `14e2d77d6e10d60d538c5a2ab7236309-778` | 2026-11-03 |
| | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_he_imagealignment.csv` | 125 | `85d2276848374482766afad40ad2c55e` | 2026-11-03 |
| | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_experiment.spatial` | 2,930 | `d88d03847832624857e2ebbc1349cf38` | 2026-11-03 |
| `atera-colorectal` | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_outs.zip` | 75,341,543,536 | `ce1dcb9e9ec1d44552586a2f7e30024a-8982` | 2026-11-04 |
| | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_he_image.ome.tif` | 5,067,976,962 | `226e11c71dcbf38b661cb2fbba09e035-605` | 2026-11-04 |
| | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_he_imagealignment.csv` | 126 | `53df92f8420d6e0934a2b22642d7baec` | 2026-11-04 |
| | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_experiment.spatial` | 2,927 | `2c402d96e0d2744898d25b0b1397e701` | 2026-11-04 |
| `atera-breast` | `Atera_v1_WTA_S4C_Breast_Cancer_FF_outs.zip` | 89,305,623,909 | `be63268c25017a86b1112d65c9263903-5324` | 2026-11-04 |
| | `Atera_v1_WTA_S4C_Breast_Cancer_FF_he_image.ome.tif` | 7,318,928,225 | `71258bd03ebd740984a3ad26cbc78580-873` | 2026-11-04 |
| | `Atera_v1_WTA_S4C_Breast_Cancer_FF_he_imagealignment.csv` | 128 | `617f03a26f8ba95f7886afdc99f325d7` | 2026-11-04 |
| | `Atera_v1_WTA_S4C_Breast_Cancer_FF_experiment.spatial` | 2,922 | `e8fff076611d29149606bd40dd79989a` | 2026-11-04 |

Each page also links `<sample>_analysis_summary.html`, a fifth object beside
the bundle, on the CDN and in the bucket. It is the report the bundle holds
as a member. The ingest records it with the other objects and reads the
report from the bundle.

| Repo | Object | Size | ETag | Last modified |
|---|---|---|---|---|
| `atera-kidney` | `Atera_v1_WTA_Kidney_non_diseased_FFPE_analysis_summary.html` | 9,114,279 | `86c92f59b074ac6d1931a060a26a4eca-2` | 2026-10-15 |
| `atera-brain` | `Atera_v1_WTA_S4A_Brain_Hippocampus_ND_FF_analysis_summary.html` | 9,387,928 | `3bc5393fa0247f3ac2881bcffdec91d8-2` | 2026-11-03 |
| `atera-colorectal` | `Atera_v1_WTA_S4B_Colorectal_Cancer_FF_analysis_summary.html` | 9,227,516 | `2b1cbe0fc7dc0450bd5af267c99c1f56-2` | 2026-11-04 |
| `atera-breast` | `Atera_v1_WTA_S4C_Breast_Cancer_FF_analysis_summary.html` | 9,225,158 | `75da29c5332603ba398a1ba22bd87da8-2` | 2026-11-04 |

## Files

The fixture branch, `fixture`, of a local repo and a local catalog, written on 2026-11-10 from the
kidney bundle (Atera Onboard Analysis 1.0.4). It holds one element of each kind. `main` holds
only the repo's first commit.

| Element | Source member | Arrays and codecs |
|---|---|---|
| `tables/table` | `cell_feature_matrix.zarr.zip`, the whole store | `X` CSR (`data` int32, `indices` int64, 1,000,000 values per chunk, `indptr`), `obs`, `var`, `obsm`; blosc-blosclz chunks copied byte for byte. The ingest adds `obs/region`, `obs/instance` and `uns/spatialdata_attrs`, and removes the index from the `column-order` of `obs` and `var`. |
| `labels/nucleus_labels/s0` | `cells.zarr.zip`, `masks/0` | `(33139, 56644)` uint32, chunks `(518, 1771)`, blosc-zstd level 1, 2,048 chunks copied byte for byte |
| `labels/cell_labels/s0` | `cells.zarr.zip`, `masks/1` | as `nucleus_labels` |
| `images/morphology/s6` | `morphology_2d/ch000{0..3}_*.ome.tif`, level 6 of each | `(4, 517, 885)` uint16, chunks `(1, 1024, 1024)`, blosc-zstd level 5 with shuffle, decoded from lossless JPEG 2000 |
| `shapes/nucleus_boundaries` | `cells.zarr.zip`, `polygon_sets/0` and `cell_id` | Iceberg table `nucleus_boundaries`; `shapes.parquet` is a `uint8` array of virtual references into the table's data file |
| `points/transcripts` | `transcripts.zarr.zip`, `grid/0,0` and `codeword_category` | Iceberg table `transcripts`, partitioned by `grid_tile`; `points.parquet` is a `uint8` array of virtual references into the table's data file |
| root attributes | `experiment.spatial`, `metrics_summary_RnD_S2_S2B_kidney_ND.csv`, this file | `title`, `description`, `source`, `documentation`, `license`, `citation`, `reading`, `sample`, `panel`, `run`, `metrics`, `missing`, `fixture`; 12.5 kB |

On the Arraylake branch `main`, `tables/table` annotates `cell_boundaries`, not `cell_labels`; see "The table's region".

The `s6` image level carries a scale of 1 in its dataset. The element transform maps it into
`global` with `0.21249222 x 33139 / 517` on y and `0.21249222 x 56644 / 885` on x.
The labels map with `0.21249222` on y and x. Points and shapes map with a scale of 1.
The output axes of `global` carry the unit `micrometer`. The input axes carry `unit`, SpatialData's placeholder.

### The table's region

The owner decided on 2026-11-11. On the Arraylake branch `main`, `tables/table` annotates
`cell_boundaries` only. It has one row per cell, 399,120 rows for the kidney. Its `region_key`
is `region` and its `instance_key` is `instance`, which equals the cell mask label.
`labels/cell_labels` and `shapes/nucleus_boundaries` join to a table row by the same `instance`.
A SpatialData table row annotates one instance of one element.
So a table on two elements would need every row twice.
The fixture's table annotates `cell_labels`, as this file records.
PLAN.md, "Stage two", lists the code change and its verify gate.

### `instance` in each table

The column `instance` has two meanings.

| Table | `instance` |
|---|---|
| `transcripts` | The row position in the element, 0-based. It names no cell. `cell_id` names the cell, and it is the points' `instance_key`. |
| `nucleus_boundaries`, `cell_boundaries` | The cell mask label, 1-based: `cell_index + 1`. |
| `obs` of `tables/table` | The cell mask label, 1-based: the row position plus one. |

### The polygon sets

Measured on 2026-11-11 in the kidney bundle. Each set is sorted by `cell_index`.

| | `polygon_sets/0` (nucleus) | `polygon_sets/1` (cell) |
|---|---|---|
| Rows | 398,197 | 399,120 |
| Unique cells | 394,076 | 399,120 |
| Cells with no polygon | 5,044 | 0 |
| Most parts of one cell | 4 | 1 |
| Cells with more than one part | 3,926 | 0 |
| Cells whose parts mix methods | 1,076 | 0 |
| `method` values (rows) | 3 (377,544), 4 (20,653) | 0 (82,577), 1 (312,076), 2 (4,467) |

Each polygon table has one row per cell that has a polygon. A cell with no polygon has no row.
So `instance` never shifts.
The source rows of one cell become one geometry, in source order.
A cell with one part is a `Polygon`. A cell with several parts is a `MultiPolygon`.
`part_count` (int32) is the number of parts. `methods` is a string of the parts' method numbers, joined by commas in part order.
An invalid ring, such as a bow-tie, stays as published.
`cell_summary` counts the parts of each cell in `nucleus_count`.
The kidney's `cell_boundaries` set has a polygon for every cell. So the `table_region` gate passes on its cell set.
The gate stays, because another sample can lack polygons.

`cell_summary` is float64 with shape 399,120 by 8, and one chunk for each column.
Its columns are `cell_centroid_x`, `cell_centroid_y`, `cell_area`, `nucleus_centroid_x`, `nucleus_centroid_y`, `nucleus_area`, `z_level` and `nucleus_count`.
`nucleus_count` has no NaN. It is 0 in 5,044 cells, 1 in 390,150, 2 in 3,741, 3 in 175 and 4 in 10.
The three nucleus centroid and area columns are NaN in the 5,044 cells with no nucleus.
The parts of set 0 equal `nucleus_count` for all 399,120 cells.
The `polygons` gate checks `part_count` against the source rows of every cell, in both sets.
It also checks `nucleus_count` for every cell in the nucleus set.

### The fixture branch

The branch `fixture` on Arraylake has several rows for one cell in `nucleus_boundaries`.
That table has an int64 `method` column.
Its `tables/table` has the region `cell_labels`. It has no `shapes/cell_boundaries`.
The gates `images`, `labels`, `matrix`, `masks`, `instances`, `copies`, `attrs`, `field_ids` and `table_index` apply to it.
The gates `tables`, `polygons`, `table_region` and `extra_tables` do not apply to it.
`verify.py --gates` runs the gates that apply.
A gate that raises prints a FAIL line with the exception, and the run goes on.

### The smaller tables

Three tables annotate no SpatialData element.
SpatialData has no element type for such a table.
So the root attribute `iceberg_tables` pins each one.
An entry has the keys of an `iceberg_table` attribute, and no `geometry_column`.
The store has no group for them under `tables/`.

| Table | Source | Index column |
|---|---|---|
| `codewords` | `transcripts.zarr.zip`: root attributes and `codeword_category` | `codeword_index` |
| `diffexp_azimuth` | `diffexp_genes/gene_expression_azimuth.csv` | `Feature ID` |
| `diffexp_graphclust` | `diffexp_genes/gene_expression_graphclust.csv` | `Feature ID` |

Both CSV members are stored with no compression. The kidney's are 22.1 MB and 14.4 MB.
Each CSV starts with `Feature ID,Feature Name`.
Then it has three columns for each group: `<group> Mean Counts`, `<group> Log2 fold change` and `<group> Adjusted p value`.
Groups differ between samples, such as `AT1`, `Amacrine cell`, `Cone-OFF-bipolar cell` and `Cluster 0`.
So the Iceberg schema comes from each CSV. Column names keep their spaces and hyphens.
Arrow infers the numeric types. The Iceberg index column is a string here, not an int64 `instance`.

### The full image elements

`images/morphology`, `images/morphology_3d` and `images/he_image` hold one array for each level of the source.
Level `k` of the source is the array `s<k>`.
Each array has one chunk for each source tile, so the chunk shape is the tile shape of that level.
Each level is one commit. A rerun skips the levels that the branch holds.
The group attributes list exactly the levels that the branch holds, so every commit is a valid store.
After the last level, the group attribute `minus80:complete` is `true`.
`--mode fixture` writes level 6 of `images/morphology` only and never sets that attribute.

- **`images/morphology`.** The four files `morphology_2d/ch000{0..3}_*.ome.tif` are four one-channel pyramids.
  Array `s<k>` is `(4, y, x)`. The channel names come from the OME-XML of the first file.
- **`images/morphology_3d`.** The member `morphology_3d/ch0000_dapi_3d.ome.tif` has one TIFF series for each plane.
  The levels of a series are the pyramid of that plane. Array `s<k>` is `(1, z, y, x)`.
  Chunks are `(1, 1, tile, tile)` and shards are `(1, z, tile, tile)`.
  The ingest decodes one tile from every plane and writes its shard once.
  The planes are 3 micrometres apart, as PLAN.md, "Axes, units, levels", states.
  The ingest gives the stack the pixel size of the 2D morphology on `y` and `x`.
- **`images/he_image`.** The objects beside the bundle are `<name>_he_image.ome.tif` and
  `<name>_he_imagealignment.csv`. The ingest decodes every level and stores it as `(3, y, x)` `uint8`.
  The kidney file has interleaved samples and Deflate tiles. The brain file has separate planes and no compression.
  The ingest reads the alignment as three rows of three numbers, separated by commas, with no header.
  It stops on any other shape.
  The matrix maps H&E pixels to morphology pixels, because its scale is the ratio of the two pixel sizes.
  The transform of the element into `global` is a sequence: the affine of the matrix, then `Scale(pixel_size)`
  with the morphology pixel size. The group attribute `alignment` holds the matrix as published.
- **Gate.** The `images` gate compares 4 sampled tiles of every stored level of each image element with tifffile.
  For the stack it compares every plane at those tiles.
  It also checks that a group marked complete holds every level of the source.

### What the fixture showed

- **Mask order.** `masks/0` is the nucleus mask and `masks/1` the cell mask, as `polygon_set_names` orders them.
  The gate sampled 50 nucleus centroids. `masks/0` label areas match `cell_summary.nucleus_area` with a median
  relative error of 0.000. `masks/1` label areas match `cell_area` with 0.000. The crossed pairs differ by 0.502 and 1.009.
- **Instance key.** The mask label at a cell's centroid is the `cell_summary` row position plus one, on 50
  of 50 sampled rows. `obs` rows follow the same order.
- **`obs.cell_id`.** It packs the cells store's `cell_id` pair as `prefix + suffix * 2**32`. Row 0 holds
  4294992761, which is the pair (25465, 1). So it is not the mask label. The ingest adds `obs/instance`,
  row position plus one, as the instance key and `obs/region` as the region key.
- **Index in `column-order`.** The source's `obs` has `_index: "barcode"`, and its `column-order`
  lists `barcode` too. `var` does the same with `feature_id`. A reader then finds the field twice.
  The ingest removes the index from `column-order` and keeps the array. `obs` then lists 18 columns
  and `var` 385.
- **Cells without a nucleus.** 5,044 of 399,120 cells have no nucleus polygon.
  `cell_summary` has NaN nucleus columns on the same 5,044 rows.
  `instance = cell_index + 1` still applies to the cells that have a polygon.
- **Invalid nucleus polygons.** 1,180 of the 398,197 nucleus polygons are not valid and not simple:
  802 ring self-intersections and 378 self-intersections. The metrics CSV leaves
  `number_of_nucleus_non_simple_polygons` empty, so the count cannot be checked against 10x's. The tables
  keep the polygons as published.
- **Unsigned integers.** Iceberg has no unsigned type, and pyiceberg maps `uint32` to the signed `int`.
  The transcripts' unassigned pair, 4294967295 and 4294967295, overflows it, so every uint32 column is
  int64 in the tables. `feature_name` is an Arrow dictionary; Iceberg stores it as a string.

### The fixture run

From a laptop, reading the bundle by range requests through 64 KiB blocks. One commit per element;
the commit message is `Write <element>`.

| Element | Read from the bundle | Wall time |
|---|---|---|
| `tables/table` | 1,168.1 MiB | 1,122.8 s |
| `labels/nucleus_labels` | 120.1 MiB | 454.0 s |
| `labels/cell_labels` | 151.7 MiB | 484.5 s |
| `images/morphology/s6` | 3.3 MiB | 12.6 s |
| `shapes/nucleus_boundaries` | 55.6 MiB | 99.7 s |
| `points/transcripts` | 32.6 MiB | 84.2 s |
| root attributes | 0.1 MiB | 0.7 s |
| outside the elements; cause not recorded | 5.7 MiB | 14.2 s |
| total | 1,537.2 MiB | 2,272.7 s |

The `X` copy and the masks read at 0.3 to 1 MiB/s, because each chunk is a separate request
from a laptop. The table read 1,168.1 MiB for 1,030.2 MiB of members: the reader fetches whole
64 KiB blocks and its cache drops some before reuse.

### The verification

`verify.py` on the fixture branch read 307.7 MiB of the bundle in 994.8 s.
The `polygons` gate fails only when the metrics CSV states a count that differs.

| Gate | Result | What it measured |
|---|---|---|
| `images` | PASS | Level 6 has one tile per channel; all 4 equal tifffile's decode. |
| `labels` | PASS | All 2,048 chunks of each mask equal the member bytes. |
| `matrix` | PASS | `X` reads as `scipy.sparse.csr_matrix`, `(399120, 24674)`, 599,212,411 stored values; 20 sampled chunks equal the members. |
| `tables` | PASS | `transcripts` 2,633,912 rows; `nucleus_boundaries` 398,197 rows; both equal the source arrays. |
| `masks` | PASS | See "What the fixture showed". |
| `instances` | PASS | 50 rows: the cell mask at the centroid is row + 1, `obs.instance` is row + 1, and `obs.cell_id` decodes to the cells store's pair. |
| `polygons` | PASS | FINDING: 1,180 invalid nucleus polygons. The metrics CSV value is empty, so the gate has no count to compare. |
| `copies` | PASS | Each `uint8` copy's bytes equal its registered Parquet file. |
| `attrs` | PASS | Each `iceberg_table` attribute's `table_uuid` and `snapshot_id` equal the catalog's. |

### Reading the fixture with SpatialData

In the virtual_omics `spatial` environment, on 2026-11-10. The repo opens with
`icechunk.Repository.open(icechunk.local_filesystem_storage(path))`, and `sd.read_zarr` reads
`readonly_session("fixture").store`.

| SpatialData | Shim | Result |
|---|---|---|
| 0.8.0, released | no | `TypeError: isinstance() arg 2 must be a type, a tuple of types, or a union`, in `_resolve_zarr_store`. The release cannot take an Icechunk store. |
| 0.8.0, released | `parquet_in_store()` | `RuntimeError: this needs spatialdata's _store module, added in PR #1107` |
| PR #1107 with the four patches | no | `ValueError: Cannot derive a filesystem for store of type IcechunkStore`, in the points reader. It raises; it does not skip points and shapes. |
| PR #1107 with the four patches | `parquet_in_store()` | `icechunk.InvalidInputError`: the `uint8` copies' chunks resolve to the warehouse's `file://` prefix, and the repo was opened without authorizing that container. |
| PR #1107 with the four patches | `parquet_in_store()`, container authorized | Opens all six elements in `global`. Images `morphology (4, 517, 885)` at scale 13.62 and 13.60; labels `(33139, 56644)` at 0.21249222; points `transcripts` 2,633,912 rows, 3D; shapes `nucleus_boundaries` 398,197 rows, index `instance`, first geometry a valid Polygon; table `(399120, 24674)` with region `cell_labels`, region key `region`, instance key `instance`. |

The shim reads the rectilinear `uint8` copies with `array.rectilinear_chunks` set.
SpatialData requires a `unit` on every space axis. Without one, it raises
`ValueError: Each axis is either of type channel either MUST have a unit.`
SpatialData reads the table keys from `uns/spatialdata_attrs`, so the ingest writes them there.
Without them, the table opens with no region and no instance key.

## Fixture findings

The fixture on Arraylake: repo `earthmover-biology/atera-kidney`, branch `fixture`, snapshot
`BF52XEQ53GH7JRBE4F30`. The ingest wrote it on 2026-11-11 in seven commits, one per element.
The rewrite on the same day added three commits; "The rewrite" gives them. The Iceberg
tables are in the namespace `atera-kidney-tables` of the catalog `arraylake:earthmover-biology`.
All of it ran on a laptop outside us-west-2.

- **(a) `add_files` on Arraylake's catalog.** Accepted. `atera-kidney-tables.transcripts`: UUID
  `9c8ad66e-9714-441a-a64c-47717a6df18e`, first snapshot 2561315328136931592, 2,633,912 rows in 1 file.
  `atera-kidney-tables.nucleus_boundaries`: UUID `92d035bd-24ee-43a6-aad6-e459230a552c`, first snapshot
  6473272151704216561, 398,197 rows in 1 file. The `attrs` gate PASS: both attributes pin the catalog's UUID and snapshot.
  The rewrite added one overwrite snapshot to each table.
- **(b) SpatialData.** The check ran in the virtual_omics `spatial` environment, on the laptop.
  It calls `sd.read_zarr` on the store of the repo's `fixture` branch, from `Client().get_repo`.
  Released 0.8.0 raises after 1.3 s on the laptop, in `_resolve_zarr_store`:
  `TypeError: isinstance() arg 2 must be a type, a tuple of types, or a union`.
  Released 0.8.0 with `parquet_in_store()` raises `RuntimeError: this needs spatialdata's _store
  module, added in PR #1107; on 0.8.0 the geometry writers derive a path from store.root and there
  is no filesystem to substitute`.
  PR #1107 with the four patches, without the shim, raises in the points reader:
  `ValueError: Cannot derive a filesystem for store of type IcechunkStore`.
  PR #1107 with the four patches, `parquet_in_store()` and `array.rectilinear_chunks` opens all
  six elements in `global`. It takes 503.0 s on the laptop.
  - images: `morphology` `(4, 517, 885)` at scale 13.62 and 13.60.
  - labels: `cell_labels` and `nucleus_labels` `(33139, 56644)` at 0.21249222.
  - points: `transcripts`, 2,633,912 rows, 12 columns, 3D.
  - shapes: `nucleus_boundaries`, 398,197 rows, index `instance`; the first geometry is a valid Polygon.
  - table: `(399120, 24674)`, region `cell_labels`, region key `region`, instance key `instance`.

  Arraylake authorizes the repo's container, so the shim needs no credentials.
- **(c) Virtual references into the managed bucket.** A repo can reference files in the org's own
  bucket only under a Virtual Chunk Access Policy. Only an org admin can add the policy.
  The policy must cover the repo's virtual chunk container.
  The container must sit at or under the policy's location.
  Without the policy, Arraylake refuses to open the repo at all, with HTTP 400:
  `Virtual chunk container prefix '<the managed bucket>' is not covered by any
  Virtual Chunk Access Policy on org 'earthmover-biology'.`
  The repo's container is the namespace's storage location.
  A private policy on the org's managed bucket has that location as its subprefix.
  With the policy, the repo opens. The `copies` gate PASS: each table has 1 `uint8` array and 1
  registered file. Each copy's bytes equal its Parquet file.
- **(d) zax on the S3-compatible endpoint.** PASS on 2026-11-11. The zax-spatialdata project ran
  its live check on branch `fixture`, snapshot `BF52XEQ53GH7JRBE4F30`. All elements open.
  The check reads through the Arraylake catalog, with vended credentials on the R2 endpoint.
  Both table UUIDs pass. Both snapshot pins pass: transcripts 5371144533979842643,
  nucleus_boundaries 3733681804133973613.
  zax reads 2,633,912 transcripts and 398,197 nucleus polygons.
  zax counts 1,180 invalid polygons, the same count as `verify.py`.
  A box query of 100 by 100 micrometres returns 4,721 transcripts, 3,143 genes and 124 nuclei.
  On a laptop, outside the source region, the check takes 23.7 s wall time and 2.1 GB of memory.
  zax reads the `feature_name` categories from the Parquet dictionary.
  zax also reads name-mapped files, so a table that another tool registers with `add_files` works.
  The fixture's AnnData table annotates `cell_labels` only. So it has no join from `nucleus_boundaries`
  to the table rows. The owner decided the join for `main`; see "The table's region".
- **(e) Throughput.** `measure.py` measured it on 2026-11-11 on one Coiled VM.
  The VM is an `m7i.xlarge`: 4 vCPU and 15 GiB, in AWS us-west-2, Coiled workspace `earthmover-devs`.
  The VM read the bundle from 10x's bucket.
  It wrote to branch `measure-2026-11-11` of `earthmover-biology/atera-kidney`.
  The VM used a personal Arraylake ID token, passed as a secret environment variable.
  The 1-vCPU rows ran under `taskset -c 0`, which pins the process and its threads to one vCPU.
  The laptop times further down do not predict the ingest.

| Part, `m7i.xlarge`, us-west-2 | vCPU | Wall | CPU, whole process | In array writes | Commit | Rate | Snapshot |
|---|---|---|---|---|---|---|---|
| `ch0000_dapi` level 0, 33139 x 56644, 1,848 tiles | 1 | 1,204.8 s | 284.5 s | 955.3 s | 2.4 s | 1.53 tiles/s, 2.97 MiB/s decoded | `H7KJHS5C0R1ZCF9CQJH0` |
| the same plane | 4 | 911.2 s | 294.4 s | 880.1 s | 2.3 s | 2.03 tiles/s, 3.93 MiB/s decoded | `X14XX29WVGB1HN1SK4Z0` |
| `cells.zarr.zip` `masks/0`, 2,048 chunks | 1 | 968.3 s | 5.8 s | not timed | 2.1 s | 2.12 chunks/s, 0.12 MiB/s | `Q8HNM2ZZ0K5C3XF48ZR0` |

  - Bytes, on the VM. The plane read 1,130.6 MiB of JPEG 2000 and decoded 3,580.3 MiB.
    It stored 1,729.0 MiB as blosc-zstd in 1,731 chunks.
    The other 117 tiles hold only the fill value, so zarr stores no chunk for them.
    The mask read 120.1 MiB and stored 120.0 MiB of chunks.
  - Checks, on the VM. Both plane runs: `check_page` PASS on 4 tiles, (0, 0), (10, 55), (21, 55)
    and (32, 55). The mask: 20 sampled chunks equal the source members, byte for byte.
  - Partial chunks. Zarr read no chunk back during any write: 0 store `get` calls.
    The tile is the chunk, so each tile write fills its chunk.
  - Plane time, 1 vCPU. 955.3 s of the 1,204.8 s wall time is inside the array writes.
    That is 0.55 s for each of the 1,731 written chunks.
    The process used 284.5 s of CPU in all, for read, decode, encode and upload.
    That is 0.15 s of CPU per tile, for decode and write together.
    The cause of the 0.55 s per chunk is not measured.
  - Mask time, 1 vCPU. 968.3 s for 2,048 chunks is 0.47 s per chunk.
    That time holds the range read from 10x's bucket and the write to Arraylake.
    `measure.py` did not time the two apart.
  - Decode threads. tifffile decodes the tiles in a thread pool of `TIFF.MAXWORKERS` threads.
    The pool had 1 thread on 1 vCPU and 2 threads on 4 vCPU.
    On 4 vCPU the plane took 24 percent less wall time.
    The array writes still took 880.1 s of the 911.2 s.
  - Not measured: the cause of the 0.55 s per written chunk; chunk writes in parallel inside one process;
    the bucket's limit on parallel writes; peak memory.
    Also not measured: the shard rewrites when the 3D stack is written plane by plane.
    Also not measured: several writer sessions on one branch, which need a merge or one branch per writer.
- **Categorical columns.** pyiceberg warns `Iceberg does not have a dictionary type. <class
  'pyarrow.lib.DictionaryType'> will be inferred as string on read.` So `feature_name` reads back
  as a plain string, not a dictionary.

The `iceberg_table` attributes on the branch:

| Element | `iceberg_table` |
|---|---|
| `points/transcripts` | `{"catalog": "arraylake", "org": "earthmover-biology", "namespace": "atera-kidney-tables", "table": "transcripts", "table_uuid": "9c8ad66e-9714-441a-a64c-47717a6df18e", "snapshot_id": 5371144533979842643, "index_column": "instance"}` |
| `shapes/nucleus_boundaries` | `{"catalog": "arraylake", "org": "earthmover-biology", "namespace": "atera-kidney-tables", "table": "nucleus_boundaries", "table_uuid": "92d035bd-24ee-43a6-aad6-e459230a552c", "snapshot_id": 3733681804133973613, "index_column": "instance", "geometry_column": "geometry"}` |

Laptop times of the ingest, reading the bundle from 10x's bucket:

| Element | Commit | Read from the bundle | Laptop wall time |
|---|---|---|---|
| `tables/table` | `MBTAEFQ7NKECAZD27V70` | 1,168.1 MiB | 4,365.0 s |
| `labels/nucleus_labels` | `TBCE647742BYK65YTH7G` | 120.1 MiB | 2,153.0 s |
| `labels/cell_labels` | `RQGYGFNECS8Y0ERWPCQ0` | 151.7 MiB | 2,121.2 s |
| `images/morphology/s6` | `HZK9EVP8AFZRCX7E1YT0` | 3.3 MiB | 17.2 s |
| `shapes/nucleus_boundaries` | `X4QMKMWWQKZ412R7YYF0` | 55.6 MiB | 118.3 s |
| `points/transcripts` | `6RM8RAD7KNATEMAM1BKG` | 32.6 MiB | 122.0 s |
| root attributes | `0F2FJWXQ847G2GSY3CB0` | 0.1 MiB | 4.5 s |
| outside the elements; cause not recorded | | 6.6 MiB | 28.2 s |
| total | | 1,538.1 MiB | 8,929.4 s |

`verify.py` against the Arraylake fixture, on the laptop: every gate PASS, 307.7 MiB read from
the bundle, 3,153.7 s wall time. The times come from timestamps on the output lines. The first
gate's time includes the bundle and repo open.

| Gate | Result | Laptop wall time | Output |
|---|---|---|---|
| `images` | PASS | 37 s | 1 of 1 tiles checked on each of 4 channels |
| `labels` | PASS | 2,967 s | 2,048 chunks of `masks/0` in 24m; 2,048 chunks of `masks/1` in 25m |
| `matrix` | PASS | 55 s | `csr_matrix`, `(399120, 24674)`, 599,212,411 stored values; 20 sampled chunks of `X/data` and `X/indices` compared |
| `tables` | PASS | 6 s | `points/transcripts` 2,633,912 rows, the source 2,633,912; `shapes/nucleus_boundaries` 398,197 rows, the source 398,197 |
| `masks` | PASS | 56 s | 5,044 of 399,120 rows have no nucleus; median relative error 0.000 for `nucleus_labels` vs `nucleus_area` and `cell_labels` vs `cell_area`; 0.502 and 1.009 crossed |
| `instances` | PASS | 13 s | 50 rows, 0 mismatches |
| `polygons` | PASS | 6 s | FINDING: 1,180 of 398,197 nucleus polygons are not valid; the metrics CSV states no count |
| `copies` | PASS | 13 s | each table: 1 `uint8` array, 1 registered file |
| `attrs` | PASS | 1 s | 2 `iceberg_table` attributes |

### The rewrite

On 2026-11-11 the zax-spatialdata project refused the fixture on three points.
The data files had no Iceberg field ids. The index of `obs` and `var` was also in `column-order`.
An `instance` column that may be null reads as float in zax.

`ingest.py --rewrite points/transcripts,shapes/nucleus_boundaries,tables/table:attrs` wrote these
elements again on the same branch, from the laptop. It read 92.2 MiB of the bundle in 117.3 s.

- Each table kept its UUID. pyiceberg refuses optional to required by default:
  `ValueError: Cannot change column nullability: instance: optional -> required`.
  With `allow_incompatible_changes`, Arraylake's catalog accepted the change, so `instance` is
  required in schema 1 of both tables.
- One overwrite snapshot in each table removes `<table>-0.parquet` and adds `<table>-1.parquet`.
  The new file carries the field id of each column, struct fields included, and a non-null `instance`.
  The old files stay in the bucket. The tables keep `schema.name-mapping.default`.
- `obs` and `var` lost the index from `column-order`; "What the fixture showed" gives the source's form.

| Commit | Element |
|---|---|
| `N8PFW9E92B57G4E722YG` | `tables/table` `obs` and `var` attributes |
| `JG1J6WZA68NE0JWKC4Y0` | `shapes/nucleus_boundaries` |
| `BF52XEQ53GH7JRBE4F30` | `points/transcripts` |

| Table | UUID | Snapshot | Data file, relative to the table |
|---|---|---|---|
| `transcripts` | `9c8ad66e-9714-441a-a64c-47717a6df18e` | 5371144533979842643 | `data/transcripts-1.parquet` |
| `nucleus_boundaries` | `92d035bd-24ee-43a6-aad6-e459230a552c` | 3733681804133973613 | `data/nucleus_boundaries-1.parquet` |

`verify.py` against Arraylake after the rewrite, on the laptop: gates `tables`, `polygons`,
`copies`, `attrs`, `field_ids`, `table_index` and `instances` PASS. It read 14.2 MiB of the
bundle in 258 s. `field_ids` found 17 field ids in the transcripts file and 10 in the polygons
file, each equal to the table schema's. `table_index` read `obs` as 399,120 rows and 18 columns,
and `var` as 24,674 rows and 385 columns.
