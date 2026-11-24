# TCGA in IDC: what the source says and how the store maps it

## Files to slides

- A TCGA slide is one DICOM series in IDC. Its files are named
  `s3://idc-open-data/<crdc_series_uuid>/<crdc_instance_uuid>.dcm`, and
  nothing in a name says whose slide it is. The barcode is the series'
  `ContainerIdentifier`, which `idc-index` exposes in `sm_index`.
- The pyramid levels are the instances whose `ImageType` has `VOLUME` as its
  third value. `THUMBNAIL`, `LABEL` and `OVERVIEW` instances are left out.
- Every level is `TILED_FULL`, so frame *i* is tile `(i // columns,
  i % columns)`.

## Offset tables

IDC's TCGA instances have an empty Basic Offset Table and no Extended Offset
Table, as checked on `TCGA-CJ-5683-01Z-00-DX1` (all four levels). To locate
the tiles, the ingest walks every fragment header, which means reading the
whole file once. The ingest downloads each level, walks it from local disk
and deletes it.

## JPEG 2000 slides are RGB, though the header says YBR

The JPEG 2000 slides declare `PhotometricInterpretation = YBR_FULL_422`.
Their codestreams set no multiple-component transform (the COD marker's MCT
flag is 0), and the decoded samples are RGB: background decodes to about
(243, 244, 243), and tissue decodes to H&E pink and purple. A reader that
trusts the header converts the RGB samples as though they were YCbCr, which
turns tissue green and background magenta. pydicom does this.

The store decodes each tile with the `imagecodecs_jpeg2k` codec and reads no
photometric interpretation, so it shows the colors the scanner recorded. The
`idc` attributes keep IDC's fields unchanged.

These files were converted from Aperio SVS by
`com.pixelmed.convert.TIFFToDicom`, as `ManufacturerModelName` records.
Whether IDC or PixelMed already tracks this issue has not been checked yet.

## Pixel spacing

Each level's scale is the `PixelSpacing` of its own instance, from
`sm_instance_index`. The resampled levels are close to, but not exactly, whole
multiples (4, 16, 32) of the base level, because the scanner software resampled them.
