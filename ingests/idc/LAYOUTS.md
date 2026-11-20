# IDC GTEx: store layout

How the GTEx slides in NCI Imaging Data Commons (IDC) map onto the store
`earthmover-biology/idc-gtex`, what every node holds, and the calls the data
did not settle. `ingest.py` builds it, `verify.py` checks it, `diagrams.py`
draws it ([source](diagrams/source.svg), [mapping](diagrams/mapping.svg),
[store](diagrams/store.svg)), and `gtex.ipynb` opens it cold. The TCGA phase
of [PLAN.md](PLAN.md) will add its own sections here when it is built.

Counts are from idc-index-data 25.0.5 (IDC release v25); file facts are from
headers read on 2026-11-20.

## The source

IDC serves GTEx from `s3://idc-open-data` (us-east-1, anonymous): one prefix
per slide series, named by its `crdc_series_uuid`, holding one DICOM object
per instance, named by its `crdc_instance_uuid`.

| What | Count |
| --- | --- |
| Slides (series) | 25,503 |
| Donors (`PatientID`) | 971 |
| Instances | 128,708: 77,702 `VOLUME`, 25,503 `THUMBNAIL`, 25,503 `OVERVIEW` |
| Bytes | 8.555 TB: 8.411 TB in `VOLUME` instances |
| Levels per slide | 3 on 24,140 slides, 4 on 1,278, 2 on 85 |
| Stain | hematoxylin and water-soluble eosin, on every slide |
| Fixative, embedding | PAXgene Tissue System, paraffin wax, on every slide |

No GTEx series holds a `LABEL` instance, so no slide label photograph is
carried; `ingest.py` refuses a series that has one rather than drop it.

**One instance per level.** Each `VOLUME` instance is one pyramid level, in
`TILED_FULL` order: frame *i* is tile `(i // columns, i % columns)` of the
total pixel matrix. Frames are JPEG Baseline, 240 x 240, one fragment each,
and the edge tiles overhang the matrix. The thumbnail and overview are one
uncompressed RGB frame each.

**The offset table is filled in.** The 9 levels of the three trial slides
carry a Basic Offset Table with one entry per frame, and on them the table's
frame ranges equal a walk of every fragment Item. A level with a table is
indexed from its header, the table and the last frame's Item header. A level
without one would still be indexed, by walking every Item through the remote
reader, at the cost of reading the file.

**Pixel size.** Each instance states `PixelSpacing` in the shared functional
groups, in millimeters: 0.4942 µm at level 0 on the trial slides
(`min_PixelSpacing_2sf` 0.00049 mm on every GTEx slide). The overview states
a spacing too, but it photographs the whole glass slide, not the scanned
region, and shares no origin with the levels.

## The store

```
/                           title, idc_version, idc_index_data, source, codecs, left_out, references_pinned_by
└── gtex/                   license (CC BY 4.0), source_DOI, citations
    └── <PatientID>/        PatientID                                          ×971
        └── <slide>/        ContainerIdentifier, PatientID, specimen, idc,     ×25,503
            │               source_prefix, levels, level_0_shape
            ├── image/      OME-Zarr 0.6 multiscale image (c, y, x), one array per VOLUME level
            ├── thumbnail/  OME-Zarr 0.6 image, one level
            ├── overview/   OME-Zarr 0.6 image, one level, axes without a unit
            └── dicom       uint8, one JSON document of every instance's header
```

### `image`

| Field | Value | From |
| --- | --- | --- |
| Levels | every `VOLUME` instance, largest first, as `0`, `1`, ... | the series listing |
| Axes | `c` (channel), `y`, `x` (micrometer) | |
| Shape | `(3, TotalPixelMatrixRows, TotalPixelMatrixColumns)` | each instance |
| Chunks | `(3, 240, 240)`, one per frame | `Rows`, `Columns` |
| Codecs | `transpose` `[1, 2, 0]`, then `imagecodecs_jpeg` as the serializer | `minus80.parsers.dicom.wsi_level_array` |
| Scale | level 0: `PixelSpacing` × 1000; coarser levels: level 0's times the ratio of the shapes | shared functional groups |
| Translation | 0 at level 0; each coarser level shifted by half the difference in pixel size, so every level's top-left corner is level 0's | |
| `omero` | RGB channels, 0 to 255 | `minus80.ome.rgb_omero` |
| `stated_pixel_spacing_um` | each level's own `PixelSpacing`, in µm | each instance |
| `source_url`, on each level | the instance's object | the listing |

The tiles are stored pixel-interleaved, `(y, x, c)`; the `transpose` codec
declares them channel-first without moving a byte. libjpeg converts each
tile's YCbCr to RGB as it decodes, within one level of pydicom's decode on
every trial tile checked.

### `thumbnail` and `overview`

One level each: the instance's single uncompressed frame, `transpose` then
`bytes`, one chunk. The thumbnail takes its stated `PixelSpacing`; the
overview's axes carry no unit and a scale of 1, because its stated spacing
describes a photograph that shares no frame with the scan.

### `dicom`

The DICOM JSON (PS3.18) header of every instance of the series, pixel data
excluded and binary elements kept inline as base64, as one UTF-8 JSON object
keyed by the node that holds the instance's pixels: `image/0`, `image/1`, ...,
`thumbnail`, `overview`. A native `uint8` array, one zstd chunk. Each header
carries a 140 KB ICC profile, which compression stores about once per slide.

### The slide group

| Attribute | Content |
| --- | --- |
| `ContainerIdentifier` | the barcode, as idc-index gives it |
| `PatientID` | the donor |
| `specimen` | `tissue` and `tissue_code` (primary anatomic structure), `stain`, `embedding_medium`, `fixative`, from idc-index `sm_index` |
| `idc` | `SeriesInstanceUID`, `StudyInstanceUID`, `crdc_series_uuid`, `license`, `source_DOI`, `series_init_idc_version`, `series_revised_idc_version` |
| `source_prefix` | `s3://idc-open-data/<crdc_series_uuid>/` |
| `levels`, `level_0_shape` | for a reader choosing slides without opening `image` |

### The root

| Attribute | Content |
| --- | --- |
| `idc_version`, `idc_index_data` | the IDC release and the catalog version the store reflects |
| `codecs` | every codec name in the store and the package that provides it: zarr, or imagecodecs for the JPEG tiles; `verify.py` refuses any other |
| `reading` | how a reader gets those codecs: `pip install imagecodecs` and `imagecodecs.zarr.register_codecs()`, or imagecodecs-zarr, which registers them automatically |
| `references_pinned_by` | how the references are pinned (below) |
| `left_out` | each slide the run could not write, with the error |

## References are pinned to ETags

Every virtual reference carries its object's ETag, taken from the series
listing, so Icechunk refuses a chunk whose object has changed instead of
reading stale offsets. VirtualiZarr's writer pins by time only;
`minus80.core.icechunk_io.write_pinned` sets the ETag. A read from the bucket
through a pinned reference was checked on the 200-slide trial store, and
`verify.py --pins` reads the stored ETag of every virtual array of the sampled
slides with `core-drill chunk-ref`.

In the IDC revision examined, a revised series got a new `crdc_series_uuid`
prefix and the old objects stayed in the bucket (PLAN.md, "Releases change
the data"). A release that revises GTEx slides becomes a commit that
rewrites their references.

## Judgement calls

1. **Hierarchy `gtex/<donor>/<slide>`.** A donor has up to dozens of slides,
   one per tissue; the donor group gathers them, and the barcode names the
   slide as IDC and GTEx do.
2. **Plain groups, OME-Zarr 0.6, no `bioformats2raw.layout`.** The slides
   share no coordinate frame, so they are not one scene.
3. **The overview has no physical unit.** See above.
4. **One header document per slide**, not one per instance: it keeps a slide
   at ten nodes and stores the repeated ICC profile once after compression.
5. **ETag pins** rather than VirtualiZarr's default time pins.
6. **Specimen facts come from idc-index's `sm_index`**, which IDC derives
   from the DICOM specimen module; the headers in `dicom` hold the source.

## Building it

`runs/01_gtex.sh` launches `coiled/ingest.sh` on one Coiled VM in us-east-1.
It lists every GTEx slide from idc-index, commits the collection and donor
groups first, then writes 500 slides per commit: each slide in its own fork,
in a pool of worker processes, and the forks merged once. A rerun skips
slides already written.
