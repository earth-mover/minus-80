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

### Chunk rows of a transcript tile

A check on 2026-11-11 measured the chunk rows of the arrays of tile `3,1`.
The columns of one tile have different chunk rows.
The array `location` has 87,382 chunk rows.
The least common multiple of the chunk rows of all columns is 45,813,334,016.
This is 43,691 times 2^20.
The ingest slices a tile in whole chunks of its widest column.

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
Each `iceberg_table` attribute of the branch pins one snapshot of `nucleus_boundaries` or `transcripts`.
The `uint8` copies of the branch reference the data files of those snapshots.
The full ingest replaces the rows of both tables in place. It deletes no snapshot and no data file.
So the pins stay valid, and the same gates pass on the branch after the replace.
A gate reads a pinned snapshot with the schema of that snapshot, not with the current schema.
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
- **`images/morphology_3d`.** The member `morphology_3d/ch0000_dapi_3d.ome.tif` is one OME series.
  Measured on 2026-11-11 for the kidney: axes ZYX, shape `(13, 33139, 56644)`, `uint16`.
  It has 13 pages, and each page has 7 SubIFDs. tifffile gives 8 levels of the series. Each level is `(13, y, x)`.
  The levels are `(13, 16569, 28322)`, `(13, 8284, 14161)`, `(13, 4142, 7080)`, `(13, 2071, 3540)`,
  `(13, 1035, 1770)`, `(13, 517, 885)` and `(13, 258, 442)` after level 0. The tiles are 1024 pixels and the compression is 34712.
  The OME `PhysicalSizeX` and `PhysicalSizeY` are 0.21249222 micrometres and `PhysicalSizeZ` is 3.0 micrometres.
  The pages of level `k` are the planes of array `s<k>`, which is `(1, z, y, x)`.
  Chunks are `(1, 1, tile, tile)` and shards are `(1, z, tile, tile)`.
  The ingest decodes one tile from every plane and writes its shard once.
  The z step of the transform is the file's `PhysicalSizeZ`.
- **Pixel sizes.** The ingest reads `PhysicalSizeX` and `PhysicalSizeY` of each 2D and 3D morphology file.
  It stops when one differs from the pixel size of `experiment.spatial` by more than a relative 1e-6.
- **`images/he_image`.** The objects beside the bundle are `<name>_he_image.ome.tif` and
  `<name>_he_imagealignment.csv`. The ingest decodes every level and stores it as `(3, y, x)` `uint8`.
  The kidney file, measured on 2026-11-11, is one page of axes YXS, `(42169, 23628, 3)` `uint8`, with 6 SubIFDs.
  Its levels after level 0 are `(21085, 11814)`, `(10543, 5907)`, `(5272, 2954)`, `(2636, 1477)`, `(1318, 739)` and `(659, 370)`.
  The tiles are 1024 pixels, the samples are interleaved, and the compression is 8 (Adobe Deflate).
  Its OME `PhysicalSizeX` is 0.27377097485986257 micrometres and `PhysicalSizeY` is 0.27377514651833773 micrometres.
  The brain file has separate planes and no compression.
- **The H&E alignment.** The kidney file has these 123 bytes, with CRLF line ends, no header and no final newline:
  `0.008566778149855193,1.288853807979006,914.1373717471324`, then
  `-1.288853807979006,0.008566778149855193,32040.595506861275`, then `0,0,1`.
  It is a 3 by 3 homogeneous matrix in `(x, y)` order. It maps H&E pixel coordinates to morphology pixel coordinates.
  The ingest reads it as three rows of three numbers, separated by commas, and stops on any other shape.
  The transform of the element into `global` is a sequence. The first step is the affine of the matrix, from `x, y`
  in H&E pixels to `x, y` in the system `morphology_pixel`. The second step is `Scale(pixel_size)` from that
  system to `global`, with the morphology pixel size.
  The matrix scale is 1.288854. Times 0.21249222 this gives 0.27387 micrometres for one H&E pixel.
  The OME pixel size of the H&E file is 0.27377 micrometres. The two differ by 0.04 percent.
  The element stores the transform only, and not the OME pixel size.
  The group attribute `alignment` holds `matrix` as published, its `axes` (`["x", "y"]`),
  and `from` and `to`: `he_image pixels` and `morphology pixels`.
- **Gate.** The `images` gate compares 4 sampled tiles of every stored level of each image element with tifffile.
  For the stack it compares every plane at those tiles.
  It also checks that a group marked complete holds every level of the source.

### The QC rasters

`images/qc_<name>` holds one raster of the analysis summary, which is a member of the bundle.
The member name differs for each sample. It starts with `analysis_summary` and ends with `.html`.
The kidney's is `analysis_summary_RnD_S2_S2B_kidney_ND.html`, 9,114,279 bytes, analysis version atera-1.0.4.1.
The ingest stops when the bundle has no such member or has more than one. It does not use `qc_images.html`.

- **The data bag.** The bag is one JSON object. It starts after the last occurrence of
  `<script id="ws-data" type="application/json">` and ends before `</script>`.
  An earlier occurrence of that text is inside an HTML comment and is not JSON.
  The ingest parses the bag with `json.JSONDecoder().raw_decode` from that position.
- **The keys.** `bag["density_plot"]` holds `bins` and `mean_q_score_80um`.
  It holds `dimensions`: `height` 7060.0 and `width` 12060.0, in micrometres.
  It holds `horizontal_bin_count`: 603.
  It holds `origin_data`: `[-10.0, -10.0]`, as `x, y`.
  It holds `unit`: `µm`.
  The ingest does not use `image_sizes` or `large_image`.
- **The resources.** A value such as `_resources_density_plot_genomic_controls` refers to
  `bag["_resources"]["density_plot_genomic_controls"]`.
  A resource is the string `data:<dtype>/zip;base64,<payload>`. The payload is one zlib stream.
  It holds little-endian values of the dtype, row-major, with row 0 at the origin.
  Rows run along y and columns run along x. All six rasters of the kidney are `uint16`.
  The ingest accepts `uint16` and `float32`. It stops on any other dtype and prints the header.
- **The grids.** The five maps of `bins` have 212,859 values each: 353 rows by 603 columns of 20 µm bins.
  The bin size is `width / horizontal_bin_count`. The rows are `ceil(height / bin size)`.
  The map `mean_q_score_80um` has 13,439 values: 89 rows by 151 columns, with the same origin.
  The bag gives no bin count for it. The bin size 80 comes from the key. The shape is `ceil(7060 / 80)` by `ceil(12060 / 80)`.
  The ingest stops when a value count does not fill its shape.
  It also stops on a raster with no value, and on a float raster with no finite value.
  In `full` mode the QC rasters are written before the images, and the report is read once.
- **The names.** A table in `qc.py` maps each label of `bins` to an element name.
  A label that the table lacks stops the ingest, so a later software version does not pass silently.
  The labels are `Transcript Density Map`, `Mean Q-Score`, `Negative Control Codewords`,
  `Negative Control Probes` and `Genomic Controls`. The names are `qc_transcript_density`, `qc_mean_q_score`,
  `qc_negative_control_codewords`, `qc_negative_control_probes` and `qc_genomic_controls`.
  The bag gives no label for the 80 µm map. Its channel name is `Mean Q-Score 80 µm` and its element is `qc_mean_q_score_80um`.
- **The element.** Array `s0` is `(1, y, x)` in the dtype of the report, in one chunk, with the image codecs.
  The channel name is the report's label. The element is complete after its one commit.
  The transform into `global` is a sequence. The first step is `Scale(1, bin size, bin size)` on `c, y, x`.
  The second step is `Translation(0, y origin, x origin)` from the system `qc_scaled`.
  Pixel `p` of an axis starts at `origin + p * bin size`.
  Bin `i` of a 20 µm map covers `[-10 + 20 i, 10 + 20 i)` micrometres.
  The other image elements use the same convention.
  A transform with a scale only places pixel 0 at 0.
- **Ranges in the kidney.** Transcript density 0 to 35,802. Mean Q-score 0 to 40. Negative control codewords 0 to 11.
  Negative control probes 0 to 3. Genomic controls 0 to 9. Mean Q-score at 80 µm 0 to 40.
- **What the density map counts.** The sum of the density map is 1,083,945,548.
  The report states 1,083,799,964 high quality transcripts. The transcript store holds 1,186,813,834 transcripts.
  The ingest does not claim what the map counts.
- **Orientation.** The ingest stores the rows as y, with no flip.
  A check on 2026-11-11 compared the map with the 2000 µm transcript tiles.
  The map's sum in each tile is 0.90 to 0.92 of that tile's transcript count.
  The 80 µm map is aligned with the 20 µm quality map.
  The density-weighted mean of the 20 µm quality map fits it best with offset zero.
- **Gate.** The `qc` gate decodes the rasters from the bundle again. It compares each stored array
  (values, dtype and shape), its scale and translation, and its channel label.

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

### The full verification

`verify.py --mode full` checks every element of the full tree on any branch. It only reads.
Every expectation comes from the bundle, so one script serves all four samples.
A gate in this mode fails on an element that the store lacks. In `--mode fixture` it skips that element.
`--repo` and `--catalog` use the form `arraylake://<org>` for the org.
Each long gate prints a progress line every five seconds.

| Gate | What it checks in the full store |
|---|---|
| `complete` | Each element of `ELEMENTS["full"]` is present. Images and labels carry `minus80:complete`. Points and shapes carry their table pin. |
| | The root attribute `iceberg_tables` has an entry for each extra table. The root attribute `missing` lists what the full mode leaves out. |
| `images` | Four sampled tiles of every level of every plane against tifffile. Every level of the source is present. |
| `qc` | Every raster of `QC_NAMES` equals the report's, with its scale and translation. |
| `labels` | Every chunk of each mask equals the source member, by hash. The stored chunk count equals the member count. |
| `matrix` | `X` is CSR with the source's shape and stored values. Every chunk of every copied array equals its source member, by hash. The metadata equals the source's `.zarray`. |
| `tables` | Each transcripts tile has the row count of `grid_number_objects`, and its `instance` range follows the source order. |
| | The `instance` ranges leave no gap across tiles. The manifests give all of this, so no data file is read. |
| | Each row group of a tile starts after the last, and its range fits its row count. The Parquet footers give this. A repeat inside one row group with the right minimum, maximum and count is not found. |
| | Each polygon table has one row per cell of its set. |
| `copies` | Each `uint8` copy has the length of its data files and the chunk edges of an 8 MiB reference. |
| | For each data file, the first block, the last block and three blocks between equal the same bytes of the file, by hash. A change in another block is not found. |
| `source` | Each source object's size and ETag from a HEAD request equal this file and the root attribute `source`. A local `--bundle` skips the gate. |

The gates read the snapshot that each attribute pins, never the current snapshot.

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

### The dry run of the full mode

On 2026-11-11, on a laptop. It used a local repo, a local catalog and a synthetic bundle.
The bundle has four morphology channels, a 3D stack, three cells, three transcript tiles and the H&E.
It measures nothing about speed. Each ingest took about three seconds.

```
uv run --package atera-ingest python ingests/atera/ingest.py --sample kidney \
  --repo <scratch>/full/repo --catalog <scratch>/full/catalog --namespace ns --mode full --branch main \
  --bundle <scratch>/data/outs.zip --he-image <scratch>/data/he.ome.tif --he-alignment <scratch>/data/al.csv \
  --concurrency 2
uv run --package atera-ingest python ingests/atera/verify.py --mode full \
  --repo <scratch>/full/repo --catalog <scratch>/full/catalog \
  --bundle <scratch>/data/outs.zip --he-image <scratch>/data/he.ome.tif \
  --gates complete,images,qc,labels,matrix,tables,polygons,table_region,copies,attrs,extra_tables,field_ids,table_index,source
```

- **Gates.** All 13 gates that ran passed. The `source` gate printed its skip line for the local bundle.
- **Gates that did not run.** `masks` and `instances` fail on this bundle. Its `cell_summary` has one column and three rows.
  The gates need `nucleus_area` and 50 sampled rows. They need the real bundle.
- **Replace.** The full ingest of an Arraylake catalog that holds the fixture's tables adds
  `--replace-tables transcripts,nucleus_boundaries`. A fresh catalog needs no flag.
- **Refusals.** `--mode full --branch fixture` and `--mode fixture --branch main` exit 1 with a message.
  `verify.py` with a local `--repo` and `--catalog arraylake://<org>` exits 1 and creates no directory.

The stop and the rerun used the same ingest command on a fresh repo and catalog.
A driver sent SIGINT to the process group after a chosen line. Each stopped run exited with code 130.

| Stop point | State at the stop | What the rerun skipped | What the rerun wrote |
|---|---|---|---|
| After `tile 0,0: 5 rows, 5 of 6` | The tile was in its table. The element had no commit. | `tables/table`, both labels, both shapes, and `tile 0,0: in the table already` | Tile `-1,0`, then every later element |
| After `images/morphology/s0: reading` | Thirteen elements held. `s0` had no commit. | The thirteen elements before `images/morphology` | `s0` to `s2` of `images/morphology`, then the later elements |

After each rerun, `verify.py --mode full` passed all 13 gates.
The comparison of each resumed store with the unstopped store gave these results:

- The node tree is the same: 65 nodes of the same kind.
- All 36 arrays have equal metadata and equal values.
- Twelve attribute keys differ. They are `snapshot_id` and `table_uuid` of the six pinned tables:
  three in the root attribute `iceberg_tables`, three in the `iceberg_table` attribute of an element.
  Each run builds its own catalog, so each run has its own ids.
- No other attribute differs. The attributes hold no timestamp.
- The six tables hold equal rows. `transcripts` has two snapshots in all three stores, one for each tile with rows.
- Each store has 33 commits and no repeated commit message. So no element was written twice.

### The OME version check

On 2026-11-11, on the `images/morphology` and `labels/cell_labels` elements of the dry-run store.
Both elements declare `ome.version` `0.5-dev-spatialdata`.

`ome-zarr-models` 1.8.1 is a dependency of the workspace. `ome_zarr_models.v05.Image.from_zarr` and
`ome_zarr_models.v05.image_label.ImageLabel.from_zarr` both reject the elements. The exception is the same for both:

```
ValidationError: 1 validation error for ImageAttrs
version
  Input should be '0.5' [type=literal_error, input_value='0.5-dev-spatialdata', input_type=str]
```

The version string is the only error. With `ome.version` set to `0.5`, `ImageAttrs` and `ImageLabelAttrs` accept the attributes.
`ome_zarr_models.open_ome_zarr` rejects both elements against every model.
Its v0.6 models say `Invalid version: '0.5-dev-spatialdata'. Must be '0.6' or '0.6.dev4'.`

SpatialData 0.8.0 from PyPI parsed the stored transforms with
`NgffBaseTransformation.from_dict` and `BaseTransformation.from_ngff`.
The transforms came from plain JSON, not from the SpatialData store reader.

| Transform | Input pixel (x, y) | Output (x, y) in micrometres |
|---|---|---|
| `images/he_image` of the dry-run store | (0, 0) | (8.500, 2.656) |
| `images/he_image` of the dry-run store | (100, 50) | (-5.196, 30.048) |
| `images/qc_transcript_density` of the dry-run store | (0, 0) | (-10.000, -20.000) |
| `images/qc_transcript_density` of the dry-run store | (3, 2) | (50.000, 20.000) |
| `write_he` with the kidney alignment and scale 0.21249222 | (0, 0) | (194.247, 6808.377) |
| `write_he` with the kidney alignment and scale 0.21249222 | (23628, 0) | (237.259, 337.344) |

Each result equals the value that the matrix and the scale give, to 1e-3.
The kidney row ran `write_he` on a small synthetic H&E with the kidney matrix, so the transform JSON is the code's own.

Booth: not checked. It needs a deployed service and the owner's direction.

## Fixture findings

The fixture on Arraylake: repo `earthmover-biology/atera-kidney`, branch `fixture`, snapshot
`BF52XEQ53GH7JRBE4F30`. The ingest wrote it on 2026-11-11 in seven commits, one per element.
The rewrite on the same day added three commits; "The rewrite" gives them. The Iceberg
tables are in the namespace `atera-kidney-tables` of the catalog `arraylake://earthmover-biology`.
The fixture ingest ran on a laptop outside us-west-2. Items (e) and (f) ran on a VM in us-west-2.

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
  - Not measured: the cause of the 0.55 s per written chunk; the bucket's limit on parallel writes.
    Item (f) measures chunk writes in parallel inside one process, and peak memory.
    Also not measured: the shard rewrites when the 3D stack is written plane by plane.
    Also not measured: several writer sessions on one branch, which need a merge or one branch per writer.
- **(f) Throughput, with concurrent writes.** `measure.py` measured it on 2026-11-11, one Coiled VM for each launch.
  The VM is an `m7g.xlarge`: Graviton, `aarch64`, 4 vCPU and 16 GiB, in AWS us-west-2, Coiled workspace `earthmover-devs`.
  `launch.py` started each VM and ran `measure.py` from the lockfile's environment in a container.
  The VM read the kidney bundle and the H&E object from 10x's bucket with range reads.
  It wrote to branch `measure-2026-11-11` of `earthmover-biology/atera-kidney`.
  It used the Arraylake machine token, passed as a Coiled secret. So the machine token can write to the repo.
  The branch does not remain.
  Each part measured a subset from the middle of its source, in its own process.
  Each part passed its check: sampled tiles against tifffile, and sampled chunks byte for byte.
  Each commit took 2.1 s to 2.5 s.

| Part (subset) | Concurrency | Wall | Rate | CPU, whole process | Peak memory | In array writes | Snapshot |
|---|---|---|---|---|---|---|---|
| `plane` (300 tiles) | 1 | 185.8 s | 1.615 tiles/s | 48.2 s | 1,299 MiB | 180.5 s | `BEA985Z63F5TYD9R2QYG` |
| `plane` | 8 | 27.2 s | 11.018 tiles/s | 48.4 s | 977 MiB | 173.5 s, summed over threads | `TCH8D49V22RVAKMDYPG0` |
| `plane` | 32 | 23.1 s | 12.982 tiles/s | 48.4 s | 900 MiB | 167.3 s, summed | `CDJYKZZVVC7B0VSZ0QSG` |
| `mask` (300 chunks) | 1 | 159.6 s | 1.88 chunks/s | 0.9 s | 310 MiB | not timed | `FEEX0VTKCSMP4JQ18S1G` |
| `mask` | 8 | 19.9 s | 15.04 chunks/s | 0.9 s | 308 MiB | not timed | `HZGDQA3SB3KSFGSVXRMG` |
| `mask` | 32 | 9.0 s | 33.40 chunks/s | 0.7 s | 312 MiB | not timed | `BDVSNM6BARHEVEKRHV20` |
| `stack` (8 positions) | 1 | 27.2 s | 0.294 positions/s | 16.0 s | 472 MiB | 9.4 s | `BQWR0JVQB7YXN127KV50` |
| `stack` | 8 | 18.2 s | 0.439 positions/s | 16.0 s | 466 MiB | 8.2 s | `H3DCV0ZMH064YQ58CVMG` |
| `stack` | 32 | 18.4 s | 0.436 positions/s | 16.0 s | 469 MiB | 8.6 s | `REB715EDYFMDB7Y7CN9G` |
| `he` (150 tiles) | 1 | 126.8 s | 1.183 tiles/s | 19.4 s | 916 MiB | 114.3 s | `6C2AVKX1ZQ23RTPQ8PKG` |
| `he` | 8 | 23.4 s | 6.419 tiles/s | 19.6 s | 954 MiB | 96.8 s, summed | `5D16GV31KPH7KW0AM7SG` |
| `he` | 32 | 17.6 s | 8.508 tiles/s | 20.6 s | 1,077 MiB | 157.2 s, summed | `ZMFSNCZCNF81MANM53K0` |

  - `plane`. 300 tiles of level 0 of `morphology_2d/ch0000_dapi.ome.tif`, from tile 774.
    The run stored 296 chunks. The other 4 tiles hold only the fill value.
    It read 206.6 MiB (216,662,016 bytes) and stored 321.5 MiB (337,140,944 bytes).
  - `mask`. 300 chunks of `masks/0` of `cells.zarr.zip`, from chunk 874, in 290 source requests.
    It read 22.5 MiB (23,592,960 bytes). The chunks hold 23,484,279 bytes.
  - `stack`. 8 shard positions of level 0 of `morphology_3d/ch0000_dapi_3d.ome.tif`, at tile row 16, from column 24.
    Each position has 13 planes, so the run made 104 tile decodes.
    It stored 8 shards, 95.0 MiB (99,607,066 bytes).
    It read 80.5 MiB (84,410,368 bytes) in 92 requests.
  - `he`. 150 tiles of level 0 of the H&E image, from tile 429.
    The run stored 450 chunks, three channels, none empty. It stored 308.5 MiB (323,531,247 bytes).
    It read 341.1 MiB (357,695,488 bytes) in 150 requests.
  - `tile`. `write_tile` on tile `3,1` of `transcripts.zarr.zip`: 90,630,655 rows, 931.2 MiB of source members by the zip's sizes.
    It wrote a local Parquet file on the VM and no table.
    It took 165.2 s: 548,733 rows/s. The process used 72.7 s of CPU and peaked at 1,299 MiB.
    It read 965.4 MiB (1,012,334,592 bytes) in 2,979 requests.
    The Parquet file is 2,230,658,968 bytes (2.08 GiB).
    Its row count equals the source's count for the tile. Its `instance` footer range is 0 to 90,630,654.
  - Concurrency 32 against concurrency 1: 8.0 times for the plane (12.982 / 1.615), 17.8 times for the mask (33.40 / 1.88),
    7.2 times for the H&E (8.508 / 1.183), and 1.5 times for the stack (0.436 / 0.294).
  - The stack decodes its planes in one thread. Its rate does not rise above concurrency 8.
  - The machine family differs from (e): `m7g.xlarge` here, `m7i.xlarge`, x86, in (e). The same region.
    This record computes no ratio between the two families.
  - Not measured: the download of the bundle to the VM's disk.
    Reads from a local file: the measurement read from the bucket, and the full run reads local files.
    The upload of Parquet files to the tables' bucket, and the table commits.
    The polygon tables. The matrix copy. The QC rasters. `verify.py --mode full`. Levels above 0.
    The checksum of a `uint8` copy on R2.
  - A staged create of a partitioned table works on the Arraylake catalog. Measured on 2026-11-11.
    An empty probe table was created with its partition spec in one metadata commit, then dropped.

  Estimate for one kidney sample, from the concurrency-32 rates. This is an estimate from rates, not a measurement.
  Level 0 has 33 rows x 56 columns = 1,848 tiles for each of 4 channels.
  The stack has 1,848 positions. The H&E has 42 x 24 = 1,008 tiles. Each mask has 2,048 chunks.
  The levels above 0 add about one third, the sum of a pyramid that halves each side. The estimate uses the factor 4/3.
  - 2D morphology: 4 x 1,848 x 4/3 = 9,856 tiles. At 12.98 tiles/s: 759 s, about 13 minutes.
  - 3D stack: 1,848 x 4/3 = 2,464 positions. At 0.436 positions/s: 5,651 s, about 94 minutes.
  - H&E: 1,008 x 4/3 = 1,344 tiles. At 8.51 tiles/s: 158 s, about 3 minutes.
  - Two masks: 4,096 chunks. At 33.4 chunks/s: 123 s, about 2 minutes.
  - Transcripts: 1,186,813,834 rows. At 548,733 rows/s: 2,163 s, about 36 minutes, for the Parquet files only.
    The tile's ratio is 24.6 bytes per row (2,230,658,968 / 90,630,655).
    So the 30 files total about 29.2 GB (1,186,813,834 x 24.6 = 29.2e9 bytes). Their upload is not measured.
  - Sum of the measured kinds: 759 + 5,651 + 158 + 123 + 2,163 = 8,854 s, about 2.5 hours.
    The unmeasured steps add to it. The 3D stack is 64 percent of the sum (5,651 / 8,854).

  Choices for the full run. The plan author made these choices from the measurement. The owner confirms them before the full run.
  - Machine: `m7g.xlarge`. The peak memory of each part is under 1.3 GiB of 16 GiB.
    The plane's CPU time is 48 s for 300 tiles. That is far below 4 vCPU x 23.1 s = 92 s, its wall time at concurrency 32.
    The stack decodes in one thread. So more vCPU do not shorten the largest step with the present code.
  - `--concurrency`: 32.
  - Disk: the bundle (45.7 GB) and the H&E (2.8 GB), plus one tile's Parquet file at a time. The largest is 2.1 GiB.
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
