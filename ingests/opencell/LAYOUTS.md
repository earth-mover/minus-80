# OpenCell: how the bucket maps onto the store

OpenCell ([Cho et al. 2022](https://doi.org/10.1126/science.abi6983)) imaged
1,310 human proteins, each tagged with split mNeonGreen2 at its own gene in a
HEK293T cell line. The raw images are in `s3://czb-opencell` (us-west-2,
anonymous, CC BY-SA 4.0 per the
[AWS Open Data registry](https://registry.opendata.aws/czb-opencell/)).

## Files

The bucket holds 12,603 objects, 912 GB: one stock `index.html` and, under
`microscopy/raw/`, one directory per gene,

```
<GENE>_<ENSG>/OC-FOV_<GENE>_<ENSG>_CID<cid, 6 digits>_FID<fid, 8 digits>_{stack,proj}.tif
```

`CID` is OpenCell's cell line ID and `FID` its field-of-view ID. FIDs are
unique across the bucket. Every field of view has both files.

| file | count | axes as stored | shape | dtype | layout |
|---|---|---|---|---|---|
| `_stack.tif` | 6,301 | ZCYX | (z, 2, 600, 600) | uint16 | ImageJ hyperstack, uncompressed, one strip per plane; the first IFD and the ImageJ description in bytes 8-383, the other IFDs after the last strip |
| `_proj.tif` | 6,301 | CYX | (2, 600, 600) | uint16 | ImageJ hyperstack, uncompressed, one strip per plane |

z depth and spacing, from a header survey of all 6,301 stacks:

| slices | z spacing (µm) | stacks |
|---|---|---|
| 51 | 0.5 | 802 |
| 101 | 0.2 | 568 |
| 106 | 0.2 | 4,019 |
| 111 | 0.2 | 247 |
| 112 | 0.2 | 596 |
| 121 | 0.2 | 41 |
| 131 | 0.2 | 28 |

Every file states an xy spacing of 0.2 µm (`XResolution` 5/1 with ImageJ
`unit` `um`). The store does not use it; see the pixel size below.

## Pixel size

The store's y and x scale is 13 / 63 = 0.2063492 µm, not the 0.2 µm the TIFFs
state.

- **Source.** The paper's Materials and Methods (supplement-1 of the NIH
  author manuscript, PMC9119736, mirrored at EBI BioStudies S-EPMC9119736),
  section "Live-cell fluorescence microscopy":

  > Cells were imaged on a DMI-8 inverted microscope (Leica) equipped with a
  > Dragonfly spinning-disk confocal system (Andor), a 63x 1.47NA oil objective
  > (Leica), and a 16-bit iXon Ultra 888 EMCCD camera (Andor, pixel size:
  > 13x13 µm2).

  A 13 µm camera pixel through a 63x objective is 13 / 63 = 0.2063 µm at the
  sample.
- **Corroboration.** CZI's SubCell evaluation data, made from the same
  bucket's projections, states 0.206349 µm per pixel.
- **The TIFFs' value.** 0.2 µm is 3% smaller and reads as rounded. It is kept,
  as stated, in each image's `tiff.resolution` (`XResolution` [5, 1]) and
  `tiff.imagej` attributes.
- **Where it is written.** `ome.multiscales[0]` scale and each array's `scale`
  attribute carry 13 / 63 for y and x. The root's `annotations.pixel_size_um`
  states the value and this source. `verify.py`'s pixel-size gate checks every
  image.
- **Assumption.** No relay lens between objective and camera changes the
  magnification. The Methods mention none. Confirming this with the OpenCell
  team is part of the plan's email.
- **z.** It is unaffected. The z step is the stage's step, 0.2 or 0.5 µm,
  stated by the TIFFs and the API alike.

## Axes

- The stack is stored ZCYX and written as (c, z, y, x) by
  `minus80.core.manifest.permute_dims`. Each chunk is one 600 × 600 plane, so
  the permutation reorders the chunk grid only and needs no transpose codec.
- The projection is written (c, y, x) as stored.
- Channel 0 is Hoechst 33342 (nuclei) and channel 1 mNeonGreen (the tagged
  protein). The TIFFs do not name their channels; the order comes from the
  [download page](https://opencell.sf.czbiohub.org/download): "The first
  channel is the signal from the Hoechst staining … and the second channel is
  the signal from the tagged protein itself."
- `_proj.tif` is the maximum over z of each channel of its stack. Checked with
  `np.array_equal` on FID 13888; `opencell.ipynb` repeats the check on one
  field.

## Hierarchy

OpenCell imaged its cell lines in 96-well plates, so the store is a set of
OME-Zarr HCS plates (0.5 §1.2, §2.7, §2.8), one per imaging plate:

```
/                                       dataset description, licence, citation, pixel size and its source;
                                        a table of every well (path, gene, cell_line_id, ensg_id, fields)
plates/<plate>                          plate: rows A-H, columns 1-12, the wells that hold images
plates/<plate>/<row>/<column>           well: one cell line; gene, Ensembl ID, how the line was made
plates/<plate>/<row>/<column>/FID<fid>  field image, array 0: the z-stack (c, z, y, x); acquisition record
plates/.../FID<fid>/proj                image, array 0: the field's projection (c, y, x)
projections/<plate>/.../FID<fid>        the same plates with only the projections, for browsing
unlisted/<GENE>/CID<cid>/FID<fid>       bioformats2raw collection: fields of the 86 unlisted lines,
unlisted/.../FID<fid>/proj              each with its projection nested the same way
```

- **Imaging plate.** It is the API's `pml_id` (`PML0116`). The cell line's own
  `plate_id` and `well_id` (`P0012`, `B12`) are where the line was made, and
  sit in the well's `cell_line` record. The imaging well equals the library
  well for 4,062 of the 5,911 listed fields.
- **Well and position.** They come from the API's `src_filename`, the original
  Micro-Manager file: `B12_14_AAMP.ome.tif` on the earlier plates,
  `MMStack_451-B12-19.ome.tif` on the later ones. All 5,911 listed fields
  parse; rows run A-H and columns 1-12.
- **Counts.** The 5,911 listed fields sit on 176 plates in 2,193 wells. Every
  well holds exactly one cell line, and every plate was imaged at one z
  depth. A cell line's fields are spread over 1 to 4 plates: 578 lines on one,
  588 on two, 137 on three and 7 on four.
- **Field names.** Each field image is named `FID<fid>`, OpenCell's
  field-of-view ID. It keeps the microscope's position number as
  `position_in_well`.
- **One image per field.** Fields of the same shape are not stacked into one
  array. An image may have only one channel or custom axis (§2.4), and `c`
  already takes that place.
- **The projection nested in its field.** The projection is derived from its
  stack, so it lives inside the stack's image group as `proj`. It can't be a
  resolution level, because it has no z. It can't be a second field of the
  well either, because a well's image paths are single names and a viewer
  would count it as another field of view. The spec names no such subgroup,
  so the field image lists it in `derived_images`
  (`{"proj": "maximum intensity over z …"}`), where `minus80.ome.hierarchy`
  and the verify gates find it.
- **`projections/`.** It repeats the projections as plates of their own, so a
  plate viewer can browse them as 2-D images. Its images reference the same
  bytes as `plates/.../proj`, so it costs only metadata and manifests.
- **`unlisted/`.** It holds the 390 fields of the 86 cell lines that are in the
  bucket but that the OpenCell website and its API do not list. The API answers
  404 for them, so there is no plate, well or acquisition record, and the
  fields are grouped by gene and cell line, with only what the path states
  (`listed_by_opencell: false`). The spec does not let a collection list
  plates among its images, so `unlisted/` is a sibling of the plate trees.
- **The root.** It holds plates and a collection side by side. OME-Zarr has no
  structure for that, so the root carries no `ome` metadata: a viewer opens a
  plate (`plates/PML0116`) or the collection (`unlisted/`), not the root. The
  root's `annotations.trees` says what each tree holds.

## Keys and joins

- **Gene symbols** in the paths match the API's `target_name` for every
  published line.
- **Ensembl IDs.** For 37 cell lines the path's Ensembl ID differs from the
  primary-assembly ID that the API and the site's tables use. Each well's `ensg_id` is the API's, and it
  keeps the path's as `ensg_id_in_path`.

  | gene | cell line | Ensembl ID in the path | Ensembl ID in the API |
  |---|---|---|---|
  | BAG6 | 9 | ENSG00000229524 | ENSG00000204463 |
  | DNAJC8 | 37 | ENSG00000285317 | ENSG00000126698 |
  | HSPA1B | 46 | ENSG00000231555 | ENSG00000204388 |
  | CANX | 149 | ENSG00000283777 | ENSG00000127022 |
  | MBOAT7 | 338 | ENSG00000278322 | ENSG00000125505 |
  | BCAR1 | 509 | ENSG00000285460 | ENSG00000050820 |
  | CTTN | 521 | ENSG00000288401 | ENSG00000085733 |
  | ARHGAP11A | 591 | ENSG00000275568 | ENSG00000198826 |
  | ABR | 605 | ENSG00000278741 | ENSG00000159842 |
  | POLR2A | 696 | ENSG00000284832 | ENSG00000181222 |
  | SEPTIN9 | 730 | ENSG00000282302 | ENSG00000184640 |
  | TJP1 | 736 | ENSG00000277401 | ENSG00000104067 |
  | NAPG | 766 | ENSG00000135631 | ENSG00000134265 |
  | CHMP4A | 775 | ENSG00000285302 | ENSG00000254505 |
  | PFDN6 | 797 | ENSG00000206283 | ENSG00000204220 |
  | LIG1 | 810 | ENSG00000144749 | ENSG00000105486 |
  | DCP1B | 835 | ENSG00000284850 | ENSG00000151065 |
  | LDHA | 945 | ENSG00000288299 | ENSG00000134333 |
  | HNRNPL | 1037 | ENSG00000282947 | ENSG00000104824 |
  | CSNK2B | 1052 | ENSG00000232960 | ENSG00000204435 |
  | BOP1 | 1061 | ENSG00000285301 | ENSG00000261236 |
  | UTP4 | 1079 | ENSG00000262788 | ENSG00000141076 |
  | SMN1 | 1109 | ENSG00000273772 | ENSG00000172062 |
  | BRD2 | 1133 | ENSG00000235307 | ENSG00000204256 |
  | RPS6KA1 | 1261 | ENSG00000281877 | ENSG00000117676 |
  | SLC16A1 | 1312 | ENSG00000281917 | ENSG00000155380 |
  | SLC37A4 | 1347 | ENSG00000281500 | ENSG00000137700 |
  | KIFC1 | 1429 | ENSG00000204197 | ENSG00000237649 |
  | PRPF8 | 1464 | ENSG00000274442 | ENSG00000174231 |
  | IPO4 | 1553 | ENSG00000285248 | ENSG00000196497 |
  | LSM14A | 1688 | ENSG00000262860 | ENSG00000257103 |
  | RUVBL1 | 1726 | ENSG00000284901 | ENSG00000175792 |
  | RPS18 | 1748 | ENSG00000235650 | ENSG00000231500 |
  | DDX39B | 1752 | ENSG00000230624 | ENSG00000198563 |
  | RAD17 | 1803 | ENSG00000276618 | ENSG00000152942 |
  | RING1 | 1828 | ENSG00000231115 | ENSG00000204227 |
  | HLA-A | 1894 | ENSG00000206505 | ENSG00000206503 |

  Looked up in Ensembl release 116 (REST `/lookup/id`, 2026-10-23), the path IDs
  are:

  - 30 IDs of the same gene on an alternate haplotype or patch (`HSCHR6_MHC_*`,
    `HG*_PATCH`), BAG6 among them;
  - 3 IDs of other genes: NAPG's path names RAB11FIP5, LIG1's names LRIG1, and
    SMN1's names its paralog SMN2 on an alternate locus;
  - 4 IDs Ensembl no longer knows (BCAR1, BOP1, DNAJC8, RUVBL1).

  The well reproduces the path's value and does not correct it.

- **Unlisted cell lines.** 86 cell lines, 390 fields of view, are in the
  bucket but answer 404 from `/api/lines/<cid>`. 85 are second lines of a gene
  that also has a published line (AKAP13: 599 published, 1932 not). One,
  HSP90B1 (CID 43), is the only line of its gene. The API has no record of
  them, so they have no plate or well, and they go in `unlisted/`.
- **Per-protein tables** on the site are keyed on Ensembl ID and gene name,
  with no cell line ID. The localization table's Ensembl IDs match the API's
  for all 1,310 published lines. The library-metadata table matches 1,300 of
  1,310 by Ensembl ID, and by gene name 1,297 exactly or 1,308 ignoring case
  (`DnaJA1` against `DNAJA1`); ATP6V0E1/ATP6V1E1 and FAM241A/C4orf32 differ
  outright. A table that goes beside the store joins on the API's Ensembl ID.

## Metadata from the OpenCell API

The cell line and field-of-view fields come from the site's JSON API
(`/api/lines?publication_ready=true` and `/api/lines/<cid>/fovs`), snapshotted
on 2026-10-23. The API is the only source for them.

- The API's `z_step_size` equals the TIFF's ImageJ `spacing` for all 5,911
  published fields of view.
- `roi_position_left` and `roi_position_top` (0 to 424) fit each TIFF being a
  600 × 600 crop of a 1,024 × 1,024 camera frame at that offset. The original
  frames are not public, so this is not checked.
- The site's viewer shows resampled stacks (`target_step_size` 0.4 µm); the
  store references the TIFFs as they are.
- Localization labels (`cytoplasmic_3`, `nucleoplasm_1`: category and grade)
  are per cell line. Every field of view's own `annotation.categories` is
  empty. The store leaves them out with the site's tables (PLAN.md).
- The imaging well equals the cell line's library well (`well_id`) for 4,062
  of the 5,911 published fields of view; the store uses the imaging well, from
  `src_filename`.
