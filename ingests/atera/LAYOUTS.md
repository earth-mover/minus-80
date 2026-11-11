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
| `tables/table` | `cell_feature_matrix.zarr.zip`, the whole store | `X` CSR (`data` int32, `indices` int64, 1,000,000 values per chunk, `indptr`), `obs`, `var`, `obsm`; blosc-blosclz chunks copied byte for byte. The ingest adds `obs/region`, `obs/instance` and `uns/spatialdata_attrs`. |
| `labels/nucleus_labels/s0` | `cells.zarr.zip`, `masks/0` | `(33139, 56644)` uint32, chunks `(518, 1771)`, blosc-zstd level 1, 2,048 chunks copied byte for byte |
| `labels/cell_labels/s0` | `cells.zarr.zip`, `masks/1` | as `nucleus_labels` |
| `images/morphology/s6` | `morphology_2d/ch000{0..3}_*.ome.tif`, level 6 of each | `(4, 517, 885)` uint16, chunks `(1, 1024, 1024)`, blosc-zstd level 5 with shuffle, decoded from lossless JPEG 2000 |
| `shapes/nucleus_boundaries` | `cells.zarr.zip`, `polygon_sets/0` and `cell_id` | Iceberg table `nucleus_boundaries`; `shapes.parquet` is a `uint8` array of virtual references into the table's data file |
| `points/transcripts` | `transcripts.zarr.zip`, `grid/0,0` and `codeword_category` | Iceberg table `transcripts`, partitioned by `grid_tile`; `points.parquet` is a `uint8` array of virtual references into the table's data file |
| root attributes | `experiment.spatial`, `metrics_summary_RnD_S2_S2B_kidney_ND.csv`, this file | `title`, `description`, `source`, `documentation`, `license`, `citation`, `reading`, `sample`, `panel`, `run`, `metrics`, `missing`, `fixture`; 12.5 kB |

The `s6` image level carries a scale of 1 in its dataset. The element transform maps it into
`global` with `0.21249222 x 33139 / 517` on y and `0.21249222 x 56644 / 885` on x.
The labels map with `0.21249222` on y and x. Points and shapes map with a scale of 1.
The output axes of `global` carry the unit `micrometer`. The input axes carry `unit`, SpatialData's placeholder.

### What the fixture showed

- **Mask order.** `masks/0` is the nucleus mask and `masks/1` the cell mask, as `polygon_set_names` orders them.
  The gate sampled 50 nucleus centroids. `masks/0` label areas match `cell_summary.nucleus_area` with a median
  relative error of 0.000. `masks/1` label areas match `cell_area` with 0.000. The crossed pairs differ by 0.502 and 1.009.
- **Instance key.** The mask label at a cell's centroid is the `cell_summary` row position plus one, on 50
  of 50 sampled rows. `obs` rows follow the same order.
- **`obs.cell_id`.** It packs the cells store's `cell_id` pair as `prefix + suffix * 2**32`. Row 0 holds
  4294992761, which is the pair (25465, 1). So it is not the mask label. The ingest adds `obs/instance`,
  row position plus one, as the instance key and `obs/region` as the region key.
- **Cells without a nucleus.** `polygon_sets/0` has 398,197 rows for 399,120 cells, so 923 cells have no
  nucleus polygon. `instance = cell_index + 1` still applies to the rows that exist. `cell_summary` has NaN
  nucleus columns on 5,044 rows.
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
| `polygons` | FINDING | 1,180 invalid nucleus polygons. The metrics CSV value is empty, so the gate has no count to compare. |
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
`0F2FJWXQ847G2GSY3CB0`, written on 2026-11-11 in seven commits, one per element. The Iceberg
tables are in the namespace `atera-kidney-tables` of the catalog `arraylake:earthmover-biology`.
All of it ran on a laptop outside us-west-2.

- **(a) `add_files` on Arraylake's catalog.** Accepted. `atera-kidney-tables.transcripts`: UUID
  `9c8ad66e-9714-441a-a64c-47717a6df18e`, snapshot 2561315328136931592, 2,633,912 rows in 1 file.
  `atera-kidney-tables.nucleus_boundaries`: UUID `92d035bd-24ee-43a6-aad6-e459230a552c`, snapshot
  6473272151704216561, 398,197 rows in 1 file. The `attrs` gate PASS: both attributes pin the catalog's UUID and snapshot.
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
  `Virtual chunk container prefix 's3://<bucket>/' is not covered by any
  Virtual Chunk Access Policy on org 'earthmover-biology'.`
  The repo's container is the namespace's location,
  `s3://<bucket>/<prefix>/`.
  A private policy on `earthmover-managed` has that location as its subprefix.
  With the policy, the repo opens. The `copies` gate PASS: each table has 1 `uint8` array and 1
  registered file. Each copy's bytes equal its Parquet file.
- **(d) zax on the S3-compatible endpoint.** Pending. The check is handed to the
  zax-spatialdata project, which reads the tables from the coordinates in this section.
- **(e) Throughput.** NOT MEASURED. The fixture ran from a laptop outside the source region, so the
  times below are laptop times only. They do not predict the ingest. A separate step measures one
  full-resolution plane and one label copy on a Coiled VM in us-west-2.
- **Categorical columns.** pyiceberg warns `Iceberg does not have a dictionary type. <class
  'pyarrow.lib.DictionaryType'> will be inferred as string on read.` So `feature_name` reads back
  as a plain string, not a dictionary.

The `iceberg_table` attributes on the branch:

| Element | `iceberg_table` |
|---|---|
| `points/transcripts` | `{"catalog": "arraylake", "org": "earthmover-biology", "namespace": "atera-kidney-tables", "table": "transcripts", "table_uuid": "9c8ad66e-9714-441a-a64c-47717a6df18e", "snapshot_id": 2561315328136931592, "index_column": "instance"}` |
| `shapes/nucleus_boundaries` | `{"catalog": "arraylake", "org": "earthmover-biology", "namespace": "atera-kidney-tables", "table": "nucleus_boundaries", "table_uuid": "92d035bd-24ee-43a6-aad6-e459230a552c", "snapshot_id": 6473272151704216561, "index_column": "instance", "geometry_column": "geometry"}` |

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
