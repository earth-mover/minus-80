# IDC whole-slide images: ingest plan

## Summary

NCI Imaging Data Commons (IDC) holds two large H&E slide collections that an
Icechunk store can reference in place:

- **GTEx**: 25,503 slides of normal tissue from 971 donors, 8.56 TB, all
  baseline JPEG, CC BY 4.0. It has no labels beyond each slide's tissue.
- **TCGA with the Pan-Cancer-Nuclei-Seg results**: 6,065 tumor slides from
  5,185 patients, 5.95 TB, with 6.17 billion nuclei outlined. Each slide
  carries a binary nuclear mask (SEG, 5.02 TB) and the nuclei's polygons
  (ANN, 1.78 TB). The slides are CC BY 3.0 and the results CC BY 4.0.

**Decided: build both, GTEx first.** GTEx is cheap and carries no open
decisions: under $5, every tile referenced. Its run tests the DICOM path at
25,000 slides, and its code, layout, figures, notebook and run script are
ready for review (*GTEx, ready to run*). TCGA with the nuclei results is the more valuable store,
for three reasons:

- it pairs every image tile with a mask tile on the same grid;
- IDC revises it between releases, so each release can become a commit;
- its files are not indexable without reading them whole.

It needs the decisions below, and its run costs about $30.

**Asked of the reviewer:** review the GTEx run and approve creating its
repo. Ian approved the calls below on 2026-11-20, as recommended, with one
change to decision 3: no codec of ours is an option, because a reader must
never need minus80 or any other code from this repository to read a store.

| # | Decision | Decided |
|---|---|---|
| 1 | Which collection runs first | GTEx, then TCGA |
| 2 | One repo per collection, or one IDC repo | One per collection: `earthmover-biology/idc-gtex` and `earthmover-biology/idc-tcga-nuclei` (repo creation needs approval) |
| 3 | TCGA's 1,716 JPEG 2000 slides whose tiles decode to YCbCr | Left out of phase 1. Later, either a JPEG transcode, or the conversion landing in a codec readers already have; never a codec from minus80 |
| 4 | TCGA's 318 JPEG 2000 slides whose tiles decode to RGB | Reference them, and state that the JVM cannot decode them yet |
| 5 | The nuclear mask: virtual or native | Native uint8 0/1, sharded, blosc zstd: about 190 GB |
| 6 | The polygons for QuPath | The ragged arrays only; no stored GeoJSON (about 5 TB) |
| 7 | A per-nucleus table in Iceberg | Not in phase 1 |
| 8 | The SEG's thumbnail-size instance | A label on the slide's thumbnail image |
| 9 | How IDC releases enter the store | One commit per IDC release, tagged `idc-v25` and so on |

## The archive

IDC converts each collection to DICOM and serves it from the public bucket
`s3://idc-open-data` (us-east-1, anonymous). A series is one prefix, named by
its `crdc_series_uuid`, with one object per DICOM instance. A slide is one
series of type VL Whole Slide Microscopy Image:

- one tiled instance per pyramid level (`ImageType` `VOLUME`);
- a `THUMBNAIL`;
- for GTEx, an `OVERVIEW` photograph of the glass slide.

Counts come from idc-index 0.12 (IDC release v25), queried on 2026-11-20.

| | GTEx | TCGA slides the nuclei results cover |
|---|---|---|
| Slides | 25,503 | 6,065 |
| Patients | 971 donors | 5,185, in 14 TCGA collections |
| Instances | 128,708 | 28,883 |
| Bytes | 8.555 TB | 5.950 TB |
| JPEG Baseline | 8.411 TB, every slide | 4.899 TB, 4,031 slides |
| JPEG 2000 | none | 1.038 TB, 2,034 slides |
| Uncompressed (thumbnail, overview) | 0.143 TB | 0.013 TB |
| Levels per slide | 3 on 24,140 slides; 4 on 1,278; 2 on 85 | 4 on 4,671 slides; 3 on 1,337; 2 on 54; 5 on 3 |
| Level-0 pixel size | 0.49 µm on every slide | 0.25 µm on 4,673; 0.50 µm on 935; others between 0.16 and 0.49 |
| Median slide | 327 MB, 1.67 gigapixels at level 0 | 848 MB, 6.62 gigapixels |
| Largest slide | 1.7 GB | 4.3 GB |
| Tiles (all levels, at 240 x 240) | about 779 million | about 725 million |
| Offset table | filled in | empty |
| License (IDC's `license_short_name`) | CC BY 4.0 | CC BY 3.0 |

Every level is one instance in `TILED_FULL` order, with one fragment per
frame. No level is split across instances, and none is `TILED_SPARSE`. Each property
was checked on the 21 levels of the 7 trial slides. On
those slides the tiles are 240 x 240, except one JPEG 2000 TCGA slide with
256 x 256 tiles.

**The nuclei results.** Stony Brook's Pan-Cancer-Nuclei-Seg
([TCIA](https://doi.org/10.7937/TCIA.2019.4A4DKP9U)) was converted to DICOM by
IDC (analysis result `pan_cancer_nuclei_seg_dicom`, CC BY 4.0,
[doi:10.5281/zenodo.11099004](https://doi.org/10.5281/zenodo.11099004)).
Each covered slide has one series of each kind. One slide has an ANN and no
SEG.

- **SEG**, 6,064 series, 5.02 TB, first published in v20. Each series is
  `BINARY` with one segment, "Nucleus", and is stored uncompressed at 1 bit
  per pixel, least significant bit first. It has 3 to 5 instances: one per
  slide level, with the same total pixel matrix, tile size and frame count as
  that level, plus one at the thumbnail's size. Frames are `TILED_FULL`. A
  240 x 240 frame is 7,200 bytes. About half the frames are empty (9,507 of
  19,600 on one slide).
- **ANN**, 6,065 series, 1.78 TB, first published in v19. Each instance holds
  one annotation group, `Nuclei`, `POLYGON`, `2D`, written by the algorithm
  `Pan-Cancer-Nuclei-Seg`. The group's arrays are stored uncompressed:
  - `PointCoordinatesData`: float32 x, y in level-0 pixels;
  - `LongPrimitivePointIndexList`: the 1-based position of each polygon's
    first value;
  - one float32 `Area` per nucleus, in µm².

  The polygons are not closed: the last vertex does not repeat the first.
  They come in runs, one per 4,000 x 4,000 pixel patch.

| Nuclei per slide | Value |
|---|---|
| Total | 6,168,609,000 (idc-index `NumberOfAnnotations`) |
| Median | 852,711 |
| Largest | 6,211,634 |
| Smallest | 8 |
| Vertices per polygon, median | 27 to 36 on 4 trial slides |

**How people use them.** GTEx samples more than 50 normal tissues, so its
slides show healthy histology across the body; IDC lists clinical and
genomic data for its donors. TCGA's slides sit beside TCGA's clinical and
genomic data in the GDC, joined by the slide barcode. The nuclei results give
an outline and an area for every nucleus the algorithm found, which is what a
nucleus segmenter trains on and what cell-level features start from.

**Licenses**, from IDC's own metadata (idc-index `index.license_short_name`
and `analysis_results_index`): GTEx CC BY 4.0
([doi:10.5281/zenodo.11099099](https://doi.org/10.5281/zenodo.11099099)); every
TCGA slide collection CC BY 3.0, with one DOI per collection; the nuclei
results CC BY 4.0. All are permissive and allow a derived store. Each
collection's DOI and license go in its group's attributes.

**Releases change the data.** IDC v25 revised 5,728 TCGA slides, every JPEG
2000 slide in TCGA, including all 2,034 the nuclei results cover. On two of
them, compared with their v24 copies (`prior_versions_index`), each `VOLUME`
instance changed in two ways:

- `PhotometricInterpretation` was corrected: `RGB` became `YBR_ICT`, or
  `YBR_ICT` became `YBR_FULL_422`;
- one `ContributingEquipmentSequence` item was added, 358 bytes.

The pixel data is unchanged but sits 358 bytes later, so every reference to
those files must be rebuilt. A revised series gets a new `crdc_series_uuid`
prefix. The old objects stay in the bucket, so an Icechunk snapshot built on
v24 still reads. GTEx has not been revised since it arrived in v19.

## Proposed layout

One repo per collection (decision 2). OME-Zarr 0.6, with plain groups above
the images, as CAMELYON17 uses. The figures (source, mapping, store) are not
drawn yet; `diagrams.py` will draw them from the trial store once the layout
is approved.

```
/                                   root: title, IDC version, licenses, citations, codecs a reader needs
└── <collection>/                   tcga_brca, ..., or gtex: license, DOI, citation
    └── <patient>/                  PatientID (TCGA-AJ-A3EK, GTEX-15DYW)
        └── <slide>/                ContainerIdentifier; tissue, IDC identifiers, source prefix
            ├── image/              (c, y, x) uint8 RGB, every level, one chunk per tile    virtual
            │   └── labels/nuclei/  (y, x) uint8 0/1, one level per image level                native   TCGA
            ├── thumbnail/          one uncompressed frame                                     virtual
            │   └── labels/nuclei/  the SEG's thumbnail-size instance                          native   TCGA
            ├── overview/           photograph of the glass slide                              virtual  GTEx
            ├── nuclei/                                                                                 TCGA
            │   ├── coords          (point, xy) float32, level-0 pixels                        virtual
            │   ├── point_index     (shape,) uint32, as DICOM stores it                        virtual
            │   ├── area            (shape,) float32, µm²                                      virtual
            │   └── offset0         (boundary,) int64, first vertex per polygon, then the total native
            └── dicom/              each instance's DICOM JSON, native UTF-8 bytes
```

**Image.** Each `VOLUME` instance is one level, largest first, with one
chunk per frame. The chunk's codec chain is `transpose`, then
`imagecodecs_jpeg` or `imagecodecs_jpeg2k`, with the codec as the serializer
(`minus80.parsers.dicom.wsi_level_array`). The pixel size comes from the
shared functional group's `PixelSpacing`, in millimeters, converted to
micrometers. Coarser levels take level 0's pixel size times the ratio of the
shapes, with translations that make every level's top-left corner coincide.
Each level's own stated spacing is kept as `stated_pixel_spacing_um`.

**Nuclear mask.** The SEG's levels sit on the image's levels exactly. In the
trial, every SEG instance's matrix size, tile size, frame count, origin,
pixel spacing and frame of reference matched its image level. The mask is
written natively as uint8, 1 inside a nucleus and 0 elsewhere, with each
image tile as an inner chunk of a 16 x 16-tile shard, compressed with blosc
zstd and bit shuffling. An empty tile is not stored and reads as 0. So a
training loader that reads `image/0` tile (r, c) reads `labels/nuclei/0`
inner chunk (r, c), and gets a 240 x 240 mask for a 240 x 240 tile. The
label's `properties` name segment 1 with the SEG's `SegmentLabel` and coded
type.

**Nuclear outlines.** The three ANN arrays are referenced in 65,536-row
chunks (`minus80.parsers.dicom.bulk_annotation_groups`). Zarr decodes only
whole chunks, so the last, partial chunk of each is written natively.
`offset0` is the layout CAMELYON's outlines use (SpatialData 0.1's ragged
layout): each polygon's first vertex as a row of `coords`, then the vertex
total. Polygon *i* is `coords[offset0[i]:offset0[i+1]]`. The ANN header goes
to `dicom/ann` with each array's value replaced by a DICOM JSON `BulkDataURI`
naming the store array, so the coordinates are not stored twice.

**What is referenced, written natively, or left out:**

- **Referenced:** every slide tile, thumbnail and overview frame, and every
  ANN array.
- **Written natively:**
  - the nuclear masks, about 190 GB;
  - `offset0`, 8 bytes per nucleus before compression, about 49 GB raw;
  - the ANN arrays' last chunks, under 1.5 MB per slide;
  - the DICOM headers, about 0.6 MB per slide, most of it each instance's
    ICC profile.
- **Iceberg:** nothing in phase 1 (decision 7).
- **Left out:** the 1,716 YCbCr JPEG 2000 TCGA slides (decision 3), and
  TCGA slides the nuclei results do not cover. Those 24,010 slides are a
  later phase, with the same code.

**Layouts considered and rejected.**

- **The SEG referenced in place, with a bit-unpacking codec.** It keeps 5 TB
  out of the store, and a frame is whole bytes. But no codec in zarr-python
  or zarr-java unpacks least-significant-bit-first packing
  ([docs/upstream.md](../../docs/upstream.md)), so every reader would need a
  codec from minus80, and QuPath would get nothing. The source's frames are
  also uncompressed: reading a masked tile in place moves 7,200 bytes, where
  the native chunk is about 800.
- **Unsharded native mask chunks.** The masks have about 725 million tiles,
  and the trial stored 20 to 45% of them. Unsharded, that is 150 to 300
  million objects, about $750 to $1,500 in PUT requests alone. With 16 x 16
  shards it is about 3 million objects.
- **One `bioformats2raw.layout` collection over all slides.** OME-Zarr 0.6's
  plain groups describe the hierarchy, and QuPath's Icechunk extension walks
  multiscale groups without the `OME/series` list.

## How each consumer reads it

| Array | QuPath, through icechunk-java | A training loader, in Python |
|---|---|---|
| `image`, JPEG (all of GTEx; 4,031 TCGA slides) | Decodes with icechunk-java's JPEG codec | Two range reads per tile pair; 0.07 to 0.13 ms to decode a tile |
| `image`, JPEG 2000 decoding to RGB (318 TCGA slides) | Not decodable: the JVM's JPEG 2000 reader has a sign bug (`scifio-jai-imageio`) | 1.0 to 1.1 ms to decode a tile, about 10 times JPEG |
| `image`, JPEG 2000 decoding to YCbCr (1,716 TCGA slides) | Left out | Left out (decision 3) |
| `labels/nuclei` | zarr-java implements blosc and sharding, untested on this store; whether the extension overlays a label image is not checked | One inner-chunk read per tile; empty tiles cost no request |
| `nuclei` ragged arrays | The extension imports outlines from CAMELYON's `coords` and `offset0`; not tested on float32 coordinates or a million polygons | `offset0` gives any polygon in two reads; the 4,000-pixel patch runs make a region's polygons contiguous |
| `thumbnail`, `overview` | Uncompressed `bytes` | Uncompressed |

## What has been tested

All runs were from a laptop, reading `s3://idc-open-data` in us-east-1, into
local Icechunk repos. No Arraylake repo or bucket was written.

**Headers, by range requests.** Header reads of 25 instances found:

- GTEx: a filled Basic Offset Table;
- TCGA: an empty one, and no Extended Offset Table;
- `TILED_FULL` everywhere, with SEG and slide levels on the same grid.

The DICOM parser now reads frame positions from a filled table
(`encapsulated_frame_ranges`). The ranges equal a full walk of every fragment
on all 21 levels of the 7 trial slides. Through a remote block reader, a
GTEx level-0 instance indexes in 0.57 to 0.70 s from the laptop, with no
download. A TCGA instance has to be read whole.

**The ANN through a remote reader.** pydicom, given a seekable remote reader,
locates all three arrays of an ANN without reading them: 1.2 to 2.1 s from
the laptop for files of 28 to 57 MB. The arrays' bytes at the computed
offsets equal pydicom's own read on all 4 trial files.

**Tile decode, JPEG against JPEG 2000.** 300 random tiles per slide, decoded
with imagecodecs on one laptop core:

| Slide | Codec | Tile | Decode per tile |
|---|---|---|---|
| GTEX-15DYW-0325 | JPEG | 240² | 0.09 ms |
| GTEX-1497J-1325 | JPEG | 240² | 0.10 ms |
| GTEX-1MCQQ-1425 | JPEG | 240² | 0.07 ms |
| TCGA-FB-AAQ3-01Z-00-DX1 | JPEG | 240² | 0.13 ms |
| TCGA-06-0210-01Z-00-DX5 | JPEG | 240² | 0.09 ms |
| TCGA-66-2765-01Z-00-DX1 | JPEG 2000, YBR_FULL_422 | 240² | 1.00 ms |
| TCGA-CG-4301-01Z-00-DX1 | JPEG 2000, YBR_ICT | 256² | 1.13 ms |

**JPEG 2000 colors.** On `TCGA-66-2765-01Z-00-DX1` (`YBR_FULL_422`), the
codestream's COD marker sets no multi-component transform. imagecodecs
returns Y, Cb and Cr; pydicom converts to RGB. The two differ by up to 194
levels. On `TCGA-CG-4301-01Z-00-DX1` (`YBR_ICT`) and on JPEG slides the two
decoders agree exactly. The parser now refuses `YBR_FULL_422` JPEG 2000 with
that reason, and the trial skipped that slide.

**Trial stores.** Three GTEx slides (tibial nerve, coronary artery, omentum)
and four TCGA slides with both nuclei series were selected. Three of the four
TCGA slides were built: two JPEG and one `YBR_ICT` JPEG 2000. From cached
copies, a GTEx slide builds in about 1 s and a TCGA slide in 5 to 9 s.

**Store check** (`verify.py`, 4 random tiles per slide; 2 s for GTEx, 8 s
for TCGA). It found 0 failures:

- every image validates as OME-Zarr 0.6;
- every tile equals imagecodecs' decode of its DICOM fragment, and is within
  1 level of pydicom's decode;
- every mask tile equals pydicom's decode of its SEG frame;
- `coords`, `point_index` and `area` equal the ANN's arrays read by pydicom,
  the native tails included;
- `offset0` indexes `coords`.

**Polygons against the mask.** Rasterizing the polygons of 5 tiles on each
of 3 slides and comparing with the SEG gives an intersection over union of
0.84 to 0.93. The overlap is higher with the polygons at their stated
coordinates than shifted by half a pixel (0.70 to 0.90). The two
representations are close but not identical. The store keeps both as
published.

**Training-loader reads** (`loader_bench.py`): random level-0 tile pairs,
image tile plus mask tile. For TCGA they were drawn from tiles with nuclei.
Image tiles came from the bucket; the trial repo, and so the mask chunks,
was on local disk.

| Store | Threads | Pairs per second | Median latency | p90 latency |
|---|---|---|---|---|
| GTEx | 1 | 10.5 | 67 ms | 153 ms |
| GTEx | 16 | 184 | 53 ms | 120 ms |
| GTEx | 64 | 510 | 67 ms | 258 ms |
| TCGA | 1 | 8.8 | 105 ms | 168 ms |
| TCGA | 16 | 216 | 53 ms | 122 ms |
| TCGA | 64 | 627 | 88 ms | 135 ms |

**Sizes.**

- A native mask's level 0 is 2.4 to 7.3 MB per trial slide. That is 3.7% of
  the SEG bytes it replaces, at about 800 bytes per stored tile.
- A GeoJSON of the polygons, as QuPath detections, would take 718 to 899
  bytes per nucleus. That is about 0.7 GB for a median slide, and about 5 TB
  for all of them.
- The trial repos hold about 0.9 MB per GTEx slide and 7 MB per TCGA slide,
  as written, before sharding.

## GTEx, ready to run

The GTEx ingest is finished and waits only for its repo. In this directory:

- `ingest.py`, `plumbing.py`: the pipeline (below);
- `verify.py`: the store check;
- `commit_bench.py`: the full-scale commit benchmark (below);
- `test_ingest.py`, `fixture.py`: the ingest end to end on two synthetic
  GTEx-like slides served over HTTP with ETags (7 tests, 4 s);
- `LAYOUTS.md`: every node and attribute;
- `diagrams.py` and `diagrams/`: the source, mapping and store figures;
- `gtex.ipynb`: the cold-open notebook;
- `runs/01_gtex.sh`, `coiled/`: the committed run.

**How it runs.** One Coiled VM, a c6in.4xlarge in us-east-1:

1. List every GTEx slide from idc-index.
2. Commit the collection and its 971 donor groups.
3. Write 500 slides per commit. Each slide is written in its own fork by
   one of 64 worker processes: the series is listed once, which gives every
   object's ETag, and each instance is read through a block reader that
   fetches its header, offset table and last Item header. The forks are
   merged once and committed.
4. Write the root's attributes, then run `verify.py`.

Processes rather than threads, because pydicom parses a header in Python:
from the laptop, 16 threads read 4.9 slides a second on about one core.
Four processes per core, because a slide's reads wait on the bucket more than
its parse runs. A rerun skips slides already written. The run needs one VM,
not many: the work is indexing a few hundred kilobytes per slide, and one
writer commits every batch.

**Measured from the laptop, against the bucket.** 200 real slides with 40
worker processes read at 10.8 slides a second, about 70 ms per request away
from us-east-1; the merge and commit of the 200 forks took 4 s. Every
reference was pinned to its object's ETag, and pinned tiles read back from
the bucket.

**A commit at full scale, locally.** `commit_bench.py` wrote the real
GTEx catalog's slides, donors, instances and level shapes (25,503 slides,
128,708 instances) with made-up offsets into a local repo, 500 slides per
commit, each merged from one fork per slide:

| Measure | Value |
|---|---|
| Nodes | 257,195 |
| References | 778,715,649 |
| Snapshot | 26.5 MB, growing about 1 MB per 1,000 slides |
| Manifests | 7.4 GB in 154,211 files, one per array per commit |
| Commit of 500 slides | median 13.8 s, 90th percentile 20.9 s; four outliers of 98, 114, 235 and 823 s, not explained |
| Merge of 500 forks | median 2.7 s |
| Opening the repo | 0.16 s |
| Walking every node's metadata | 52 s |

The snapshot holds every node, so it grows with the slide count, and a
reader fetches all of it on open: 26.5 MB for GTEx, inside Icechunk's
default cache of 500,000 nodes. Commit time did not grow with the repo: the
last ten batches took as long as the first ten. The outliers are a reason
to watch the run's log: a commit past a minute is investigated, not waited
out.

**Verify gates.** `verify.py` checks every node in one walk of about a
minute at full scale: completeness against idc-index, every array's codecs
against the root's `codecs` list, and each slide's levels, units and pixel
size. On 10 sampled slides it compares a tile with its DICOM fragment and the
thumbnail with pydicom's decode. The `codecs` list holds only codecs that
zarr itself or imagecodecs-zarr registers: `bytes`, `transpose`, `zstd`,
`blosc`, `imagecodecs_jpeg` and `imagecodecs_jpeg2k`. No reader needs code
from this repository. `--pins` reads the stored ETag of every virtual array of
the sampled slides with `core-drill`, because Icechunk's Python API does not
expose a stored reference; it runs from a laptop after the job. All gates
passed on the 200-slide trial store.

**Cost of run 1.**

| Item | Estimate |
|---|---|
| VM | c6in.4xlarge at about $0.91 an hour, for about an hour of indexing and commits, plus verify: about $1 |
| Reads from the bucket | about 20 GB of headers, in-region, no egress charge |
| Writes to Arraylake | about 180,000 manifest and chunk objects and about 25 GB, under $2 |
| Storage | about 25 GB, under $1 a month |
| Total | under $5 |

## Judgement calls

1. **GTEx first.** It has no open calls and every tile is JPEG. It costs
   under $5 and can run as soon as its repo exists.
2. **One repo per collection.** GTEx and TCGA are different datasets, with
   different licenses and different consumers. A single IDC repo would add
   nothing a reader needs. GTEx comes to about 380,000 nodes and TCGA's
   phase 1 to about 130,000.
3. **YCbCr JPEG 2000 left out of phase 1.** Referencing those tiles needs a
   YCbCr-to-RGB step that no Zarr codec provides. A codec in minus80 is ruled
   out: a reader must never need code from this repository to read a store.
   Two ways forward remain:
   - transcode the 1,716 slides to JPEG natively, priced under *Cost*;
   - wait for the conversion to land in a codec readers already have, such
     as an option on imagecodecs' JPEG 2000 codec to apply the inverse color
     transform.

   The store's root attributes list the left-out slides and the reason.
4. **RGB JPEG 2000 referenced.** The 318 slides decode correctly in Python.
   QuPath waits on the JVM decoder, which affects these slides only.
5. **Native mask.** See *Layouts considered and rejected*.
6. **No stored GeoJSON.** At about 5 TB, a GeoJSON copy would be the
   largest part of the store. QuPath reads outlines from ragged arrays
   through the Icechunk extension. A per-slide GeoJSON can be written on
   demand from `offset0` and `coords`.
7. **No per-nucleus Iceberg table yet.** The polygons and areas are arrays
   with a shared index, not a table without a Zarr convention. A table of
   centroids and bounding boxes for 6.17 billion nuclei would make
   cross-slide queries possible, at about 75 GB. Add it when a consumer asks.
8. **The SEG's thumbnail instance becomes a label on the thumbnail.** It has
   the thumbnail's size, so it is that image's label. The trial left it out;
   phase 1 adds it.
9. **One commit per IDC release.** The first build records `idc_version`
   `v25` and is tagged `idc-v25`. A later release becomes:
   - a commit that rebuilds the references of revised series;
   - new slides added;
   - a new tag.

   Earlier snapshots keep reading, because IDC keeps the prior objects.
10. **Changes shared through minus80.**
    - `wsi_level_array` refuses YCbCr JPEG 2000, where it used to reference
      tiles that read as wrong colors.
    - `encapsulated_frame_ranges` uses a filled offset table, and every
      DICOM parser function reads a seekable remote file as well as a path.
    - `icechunk_io.write_pinned` pins each reference to its object's ETag.

    Each is tested: the parser on synthetic files, the pins against a local
    HTTP server that changes an ETag.

## Steps, scope and risk

### Runs

Each run is a committed `runs/NN_*.sh` on Coiled in us-east-1, beside the
bucket. Each slide is written in its own fork by a worker process, and one
writer merges the forks and commits.

1. **GTEx** (`runs/01_gtex.sh`). Index every instance from its header and
   offset table through a remote reader, without downloading pixels. Commit
   500 slides at a time, each merged from its slides' forks. Verify, then
   tag `idc-v25`. Details under *GTEx, ready to run*.
2. **TCGA nuclei, JPEG and RGB JPEG 2000** (`runs/02_tcga_nuclei.sh`), 4,349
   slides. Download each slide's levels to walk their fragments, and each
   SEG to unpack it. Locate the ANN arrays through a remote reader and read
   only their tails and `LongPrimitivePointIndexList`. Write sharded masks,
   then verify and tag.

### Cost

Rough figures, assuming c6in VMs at on-demand prices and 1.5 GB/s per VM
from the bucket. The VM rate is the SanctSound figure in
[docs/formats.md](../../docs/formats.md), not a measurement on this bucket.

| Run | Reads from the bucket | Compute | Native writes | Storage per month |
|---|---|---|---|---|
| GTEx | headers and offset tables, about 20 GB | under 1 VM-hour, under $2 | about 20 GB of manifests and headers | under $1 |
| TCGA nuclei, phase 1 | slides 4.9 TB, SEG about 4 TB, ANN headers and indexes | 4 to 8 VM-hours on c6in.8xlarge, $10 to $20; with merging and retries, $30 | masks about 160 GB in about 2.5 million shards, $13 of PUT requests; `offset0` and headers about 40 GB | about $5 |
| JPEG 2000 transcode, 1,716 slides (if chosen) | 0.83 TB | about 1 ms decode and 0.2 ms encode per tile, 170 million tiles: 60 core-hours, about $5 | about 0.8 TB of JPEG tiles in about 700,000 shards | about $19 |

The TCGA run is over the $10 threshold, so it waits for a go-ahead. No run
approaches the threshold for announcing spend. The GTEx line is refined
under *GTEx, ready to run*.

### Writes that need approval

- creating `earthmover-biology/idc-gtex` and
  `earthmover-biology/idc-tcga-nuclei`;
- a virtual chunk container for `s3://idc-open-data/` on each, anonymous;
- write grants for the ingest token.

### Risks

- **Commit and snapshot size.** GTEx's full count of references and nodes
  was committed locally (*GTEx, ready to run*), not to Arraylake, whose
  per-object latency makes each commit slower. The first batch's commit time
  in the run's log is the check: over a minute per 500 slides would stretch
  the run past its estimate.
- **In-region rates.** Every rate here was measured from a laptop. The
  TCGA run reads about 9 TB. Its estimate holds only if a VM sustains about
  1.5 GB/s from this bucket, which a short probe checks first.
- **QuPath.** Nothing here was opened in QuPath. Three things are untested:
  the extension's handling of float32 `coords`, a million polygons on one
  slide, and label overlays.
- **The nuclei mask and polygons disagree** on 7 to 16% of the pixels either
  marks as nucleus, per tile. That is a property of the published data,
  to be recorded in `LAYOUTS.md`, not corrected.
- **IDC can revise again.** A revision moves byte offsets, so a store
  without a new commit keeps reading the prior objects. If IDC ever deletes
  prior versions from the bucket, old snapshots break.
- **The 240 x 240 tile and 0.25 µm pixel figures** come from samples; the
  full run reads every header and refuses anything outside the parser's
  limits, with the reason.
