# 3D-4i zebrafish embryos (S-BIAD3228): how the source maps onto the store

The source is one zip, `AssayPlate_Greiner_655090.zarr.zip`, holding an
OME-Zarr 0.4 HCS plate in Zarr v2 written by Fractal. PLAN.md gives the
decisions; this file records the mapping and what the source taught us.

## Mapping

| Source | Store |
|---|---|
| `<row>/<col>/<round>/<level>`: one well-sized image per round, `(c, z, y, x)` uint16, every field pasted at its stage position | `<row>/<col>/<image>/<level>`: one image per field and round, cut from the well image |
| field `FOV_n` in round `r` | image `4 * (n - 1) + r` |
| `<row>/<col>/1/labels/<name>/<level>`, `(z, y, x)` uint32 | `<row>/<col>/<image of round 1>/labels/<name>/<level>`, cut to the field |
| `FOV_ROI_table`: one row per field | the images themselves; each row's position is the image's translation in the well's `scene`, and the row is kept in the image's `annotations` |
| `well_ROI_table`, `embryoRaw_ROI_table` | kept as rows in the well group's `annotations` |
| plate `acquisitions` 0 to 2 | acquisitions 0 to 3, each described by its channels |

Level 0 of a field is its exact 1000 x 1000 window. Levels 1 to 3 are the
source's own pixels: the window covers every coarse pixel the field touches,
so it can be one pixel wider than 1000 / 2^k, and each level's translation
says where it sits.

## What the source taught us

- **Round 3 is missing from the HCS metadata.** Every well holds image groups
  `0` to `3`, but the plate lists acquisitions 0 to 2 and each well lists
  images 0 to 2, so a reader that follows the metadata never finds round 3
  (DAPI_3, Nanog, YAP, XRN2). The store lists all four; the original list is
  kept as the plate's `source.source_acquisitions`. Worth reporting to the
  authors.
- **`FOV_ROI_table` rows are in no fixed order, and the order differs between
  rounds.** In well C/02, rounds 0 to 2 list FOV_1, FOV_7, FOV_4, FOV_6,
  FOV_3, FOV_5, FOV_2; round 3 lists FOV_3, FOV_2, FOV_5, FOV_1, FOV_6, FOV_7,
  FOV_4. The values agree field by field. The ingest matches rows by name and
  numbers images by field number, never by row position.
- **Image levels are 2 x 2 averages, labels keep one pixel.** Compared on one
  grid cell of C/02 round 1 (planes 130 to 139): `nucleiRaw` level 1 equals
  the bottom-right pixel of each 2 x 2 block of level 0 on all 2.5 million
  pixels; DAPI level 1 is within 0.09 grey levels on average of the 2 x 2
  mean, though no simple rounding reproduces it exactly. Either way coarse
  pixel `j` stands for the block of level-0 pixels from `2j`, so the same
  translation applies to images and labels.
- **The space between fields was never imaged.** It is exactly 0 in every
  level; inside a field, background is camera noise of 0 to 8. Level 0 stores
  no chunk that misses every field; levels 1 to 3 store all-zero chunks
  across the whole well image.
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

The plate's `field_count` is the most fields any well holds (9), not the most
images (36): rounds are acquisitions, not fields.
