# 3D-4i zebrafish embryos (S-BIAD3228): how the source maps onto the store

The source is one zip, `AssayPlate_Greiner_655090.zarr.zip`, holding an
OME-Zarr 0.4 HCS plate in Zarr v2 written by Fractal. PLAN.md gives the
decisions; this file records the mapping and what the source taught us.

## Mapping

| Source | Store |
|---|---|
| `<row>/<col>/<round>/<level>`: one well-sized image per round, `(c, z, y, x)` uint16, 4 channels, every field pasted at its stage position | `<row>/<col>/<image>/<level>`: one image per field, cut from the well images of all four rounds, 16 channels |
| field `FOV_n` | image `n - 1` |
| channel `c` of round `r` | channel `4 * r + c` of the field's image; its name, color, window and `wavelength_id` are the source's, and the image's annotations record `r` and `c` |
| `<row>/<col>/1/labels/<name>/<level>`, `(z, y, x)` uint32 | `<row>/<col>/<image>/labels/<name>/<level>`, cut to the field; the annotations record round 1 |
| `FOV_ROI_table`: one row per field | the images themselves; each row's position is the image's translation in the well's `scene`, and the row is kept in the image's `annotations` |
| `well_ROI_table`, `embryoRaw_ROI_table` | kept as rows in the well group's `annotations` |
| plate `acquisitions` 0 to 2 | no acquisitions, since every image holds all four rounds; the plate's `source.rounds` lists each round's channels, and `source.source_acquisitions` keeps the source's list |

Level 0 of a field is its exact 1000 x 1000 window. Levels 1 to 3 are the
source's own pixels: the window covers every coarse pixel the field touches,
so it can be one pixel wider than 1000 / 2^k, and each level's translation
says where it sits.

## What the source taught us

- **Round 3 is missing from the HCS metadata.** Every well holds image groups
  `0` to `3`, but the plate lists acquisitions 0 to 2 and each well lists
  images 0 to 2, so a reader that follows the metadata never finds round 3
  (DAPI_3, Nanog, YAP, XRN2). Every image in the store holds all four
  rounds' channels; the original list is kept as the plate's
  `source.source_acquisitions`. Worth reporting to the
  authors.
- **`FOV_ROI_table` rows are in no fixed order, and the order differs between
  rounds.** In well C/02, rounds 0 to 2 list FOV_1, FOV_7, FOV_4, FOV_6,
  FOV_3, FOV_5, FOV_2; round 3 lists FOV_3, FOV_2, FOV_5, FOV_1, FOV_6, FOV_7,
  FOV_4. The values agree field by field. The ingest matches rows by name and
  numbers images by field number, never by row position.
- **Image levels are 2 x 2 averages, labels keep one pixel.** Compared on one
  grid cell of C/02 round 1 (planes 130 to 139): `nucleiRaw` level 1 equals
  the bottom-right pixel of each 2 x 2 block of level 0 on all 2.5 million
  pixels; DAPI level 1 is within 0.09 gray levels on average of the 2 x 2
  mean, though no simple rounding reproduces it exactly. Either way coarse
  pixel `j` stands for the block of level-0 pixels from `2j`, so the same
  translation applies to images and labels.
- **The space between fields was never imaged.** It is exactly 0 in every
  level; inside a field, background is camera noise of 0 to 8. Level 0 stores
  no chunk that misses every field; levels 1 to 3 store all-zero chunks
  across the whole well image.
- **1,244 chunks are empty files.** Every well has some, from 10 in the
  fewest to 94 in F/04: 139 image chunks at level 0, 479 at levels 1 to 3,
  and 626 label chunks. Level 0 stores only chunks that touch a field, so the
  empty level-0 chunks are pixels missing inside embryos: each is 10 planes of
  one channel over one 1000 x 1000 grid cell. `C/02/1/labels/cells/0/26/0/7`
  is one. No reader can decode an empty chunk, so the source fails there for
  any tool. The store reads each as the fill value, 0, as the repository's
  rule for a unit with no stored bytes requires, and each image's
  `annotations.empty_source_chunks` lists the empty chunks inside its field.
  Worth reporting to the authors and the BioImage Archive, with the round-3
  omission: only they can say whether the data were lost in processing or in
  packing the zip.
- **Every member of the zip is followed by a data descriptor,** a 16-byte
  record of its sizes, and past 4 GB each directory entry's extra field is
  21 bytes while its local header's is 9. VirtualiZarr's zip parser then reads
  all 2 million local headers; `minus80.parsers.zip.members` places the
  members from the gaps instead (docs/upstream.md).
- **Every embryo carries label 1 in `embryoRaw`,** so `embryoRaw_ROI_table`
  has one row per well whose box spans the well, not one per embryo.
- **`x_micrometer_original` is about half of `x_micrometer`** in every row,
  and likewise for y. Unexplained; both are kept.
- **The plate declares rows B to G and columns 02 to 07 only**, the wells in
  use, not the 96-well plate's full grid. The store keeps the source's grid.
- **No field touches a coarse pixel the source did not store.** Each level's
  size is rounded down, so a field on the far edge of its well image could
  reach past the last coarse pixel; none of the 211 does. The ingest stops
  such a window at the level's edge.

## Plate `field_count`

The plate's `field_count` is the most fields any well holds (9), which is
also the most images, since each image is one field.
