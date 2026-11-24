# TCGA whole-slide images: plan

## Summary

This plan puts the H&E diagnostic slides of The Cancer Genome Atlas (TCGA) in
one Icechunk store, `earthmover-biology/tcga` on Arraylake. NCI Imaging Data
Commons (IDC) publishes the slides as DICOM, and the store references IDC's
tiles where they sit. The first ingest covers the DX1 slide of each of the
first 100 TCGA-KIRC (kidney clear cell carcinoma) patients. It is a minimum
store to build on: later ingests add the remaining slides, more collections
and the clinical data.

Decisions taken:

- **Scope:** 100 diagnostic slides from one collection.
- **Layout:** a browsable hierarchy, `/<collection>/<patient>/<slide>/image/`.
- **Repo:** `earthmover-biology/tcga`, Icechunk spec version 2. It reads from
  the existing `idc-open-data` bucket configuration.

## The archive

IDC republishes TCGA's slides in the public bucket `s3://idc-open-data`
(us-east-1, anonymous access). The source used here is IDC v25, as indexed by
the `idc-index` 0.13.5 package. Its license is CC BY 3.0.

| | TCGA-KIRC in IDC v25 |
| --- | --- |
| Patients | 537 |
| Slide series | 2,173 (1,654 frozen sections, 519 FFPE diagnostic) |
| Diagnostic (DX) slides | 519 from 513 patients, 486 GB |
| Encoding of DX slides | 380 JPEG 2000, 139 JPEG Baseline |
| Base pixel spacing of DX slides | 0.25 µm on 499, about 0.5 µm on 20 |

Each slide is one DICOM series. Each pyramid level in it is one instance, a
file named by a random UUID, holding 240 × 240 tiles in `TILED_FULL` order. A
slide has three or four levels plus a thumbnail. No file says which patient
or slide it belongs to; only IDC's index joins a UUID to a slide barcode.

## Proposed layout

```
/                                    description
/tcga_kirc/                          collection, description, source, source_doi, license
/tcga_kirc/TCGA-A3-3365/             patient_id, collection
/tcga_kirc/TCGA-A3-3365/TCGA-A3-3365-01Z-00-DX1/     idc: the slide's row of IDC's index
    image/                           OME-Zarr 0.6 multiscale image, RGB omero
        0  (3, 25731, 26858) uint8   chunks (3, 240, 240), 0.252 µm
        1  (3, 6432, 6714)   uint8   chunks (3, 240, 240), 1.008 µm
        2  (3, 1608, 1678)   uint8   chunks (3, 240, 240), 4.034 µm
```

- Every chunk is one DICOM tile, referenced in IDC's bucket and never copied.
  A frame with no stored bytes is a missing chunk.
- Each level's scale is the pixel spacing its own instance states, converted
  from millimeters to micrometers.
- `idc` holds the slide's identifiers (`SeriesInstanceUID`,
  `crdc_series_uuid`, the barcode), its scanner, stain, fixative, embedding
  medium, anatomy, objective power, source DOI and license.
- No Iceberg table yet. A table with one row per slide, joined to TCGA's
  clinical data, is the next step.

One layout was considered and rejected: flat slide groups with the cancer type
and patient held only in attributes. That layout needs attribute queries to be
usable, and Icechunk 2 has none.

## What has been tested

Two TCGA-KIRC slides were ingested from a laptop into a local repo, using this
ingest's code (2026-11-24):

- `TCGA-A3-3365-01Z-00-DX1`, JPEG 2000, 47 MB, and `TCGA-AK-3460-01Z-00-DX1`,
  JPEG Baseline, 200 MB. Each took 5 to 8 s to download and parse.
- Every sampled tile of the JPEG Baseline slide equals pydicom's decode,
  across all levels.
- The JPEG 2000 slide decodes to correct H&E colors. pydicom's decode differs
  from it because pydicom applies the header's YBR color conversion to tiles
  that are already RGB (LAYOUTS.md).
- Tiles read back from the bucket in 50 to 350 ms each from the laptop.

## Judgement calls

- **Which slides.** The DX1 slide of the first 100 patients in barcode order,
  so a rerun selects the same slides. All 100 have a 0.25 µm base level, and 73
  are JPEG 2000.
- **Slide names.** The full slide barcode (`TCGA-A3-3365-01Z-00-DX1`), not the
  short `DX1`, so a slide group still names itself when it is read out of
  context.
- **Colors of the JPEG 2000 slides.** The store decodes the codestream as it
  is stored and ignores the header's photometric interpretation. The header's
  value is kept in the source and recorded in LAYOUTS.md.

## Steps, scope and risk

1. Create `earthmover-biology/tcga` with a virtual chunk container on
   `s3://idc-open-data/`. Done, with approval.
2. Grant the ingest's API token write access to the repo. This needs approval
   and is done in the Arraylake web interface.
3. Run `runs/01_kirc_first_100.sh`. It runs on one m6i.4xlarge in us-east-1,
   downloads about 58 GB within the region to find the tiles, and commits
   every 10 slides to `main`. The run should take under 30 minutes and cost
   under $2.
4. Check the store, open it in napari, and write `REPORT.md`.
