# OpenCell: ingest report, phase 1

## What was built

OpenCell's live-cell images are in the Arraylake repo
`earthmover-biology/opencell`, as a virtual OME-Zarr 0.5 store in Icechunk.
Every chunk references a strip of a TIFF in OpenCell's public bucket,
`s3://czb-opencell`, so no pixels were copied or decoded. The repo is private
to the organization.

- **Snapshot.** `XADEMEKMPVBQX6V4KWC0` on `main` holds the images.
  `VM5ZAD68VMZAYV6C6KH0` corrects the root's description and changes nothing
  else.
- **Contents.** All 12,602 TIFFs in the bucket, as 6,301 fields of view:
  - 5,911 fields of the 1,310 cell lines the OpenCell website lists, in 2,193
    wells on 176 HCS plates under `plates/`;
  - each field's maximum projection nested in its field image as `proj`, and
    repeated under `projections/` as plates of their own;
  - 390 fields of the 86 cell lines the website does not list, in the
    `unlisted/` collection.

  That makes 18,513 images, each with one resolution level.
- **Layout.**

  ```
  /                                         title, citation, licence, pixel size and its source,
                                            what each tree holds, a table of every well
  plates/<plate>/<row>/<column>/FID<fid>    z-stack (c, z, y, x), with the field's acquisition record
  plates/.../FID<fid>/proj                  the field's projection (c, y, x)
  projections/<plate>/.../FID<fid>          the same projections, as plates of their own
  unlisted/<GENE>/CID<cid>/FID<fid>[/proj]  the unlisted lines' stacks and projections
  ```

  LAYOUTS.md explains each level and every key.
- **Cost.** One run from a laptop in California, outside AWS, took 9 min 7 s:
  parsing the 12,602 TIFFs took 360 s, writing the 6,301 fields 67 s and the
  commit 115 s.
- **Opening it.** `opencell.ipynb` opens the store with only `arraylake`,
  `zarr` and `matplotlib`. It finds a protein's wells from the root's table,
  reads a field's metadata and projection, draws an x–z section at true scale,
  and checks that the projection is its stack's maximum over z. Run it with
  `uv run --package opencell-ingest --group notebook jupyter lab`.

## Verification

`verify.py` ran against snapshot `XADEMEKMPVBQX6V4KWC0`. All 18,513 images pass
every metadata gate:

- the shared gates in `minus80.ome.verify`: OME-Zarr 0.5 schema, dimension
  names, a plausible physical size, every declared image present and every
  image declared, levels ordered;
- completeness: every field of view in the bucket is in the store, each listed
  field at its plate, well and field with its projection nested and in
  `projections/`, and each unlisted field in `unlisted/`;
- each stack's z scale equals the z step the OpenCell API states for it;
- every image's y and x scale is 13 / 63 µm;
- each well's Ensembl ID is the API's.

A sample of 12 fields, one for each z depth on the plates (7) and in
`unlisted/` (5), covers every layout in the store. On those 31 images (the 12
stacks, their 12 nested projections and the 7 matching `projections/` images),
every chunk reference read back from the store has the path, offset and length
of its strip in tifffile's index of the file. The middle z plane of each stack
and the whole of each projection equal tifffile's decode.

Before the write, all 12,602 files parsed with no refusals, and every parsed
file matched a tifffile header survey on shape, dtype, axes, z spacing and
units, with every chunk reference inside its object.

The store's projections are OpenCell's own `_proj.tif` files. `opencell.ipynb`
checks on one field that a projection is its stack's maximum over z.

## Changes from the plan

- **Commit concurrency.** The first run stalled in the commit. An Arraylake
  repo's stored config keeps Icechunk's commit manifest concurrency at 1, so
  the commit wrote about 18,500 manifests one round trip at a time.
  `minus80.core.icechunk_io.arraylake_repository` now opens Arraylake repos
  with a concurrency of 32, and the commit took 115 s.
- **Verification on a sample.** The plan's verification checked every chunk
  reference and read every file's directories, which took hours from a laptop
  against the published repo. References and pixels are checked on a sample
  covering each layout instead, and the metadata gates still cover every image.
- **Commit cost.** The plan asked for the commit to be timed on an own-prefix
  bucket before the Arraylake write. The Arraylake commit itself was the
  measurement: 115 s for about 18,500 arrays.
- **The root description.** Opening the store cold showed that the root's
  description named a `z-stacks/` tree, a name the layout dropped before the
  run; the tree is `plates/`. Snapshot `VM5ZAD68VMZAYV6C6KH0` corrects it,
  and `ingest.py` writes the corrected text.
- **Plate viewer.** The plan's step 4 included opening the store in a plate
  viewer. That has not been done.

## Judgement calls made

The plan's judgement calls 1, 2, 3 and 6 were decided before the run, and
phase 1 follows them. Call 4 was taken as recommended.

| # | Call | As built |
|---|---|---|
| 1 | The maximum projections | Nested in each field image as `proj`, listed in its `derived_images`, and repeated in `projections/` |
| 2 | The 86 cell lines the website does not list | The `unlisted/` collection, beside the plates, with only what each file's path states |
| 3 | Hierarchy and keys | HCS plates from the API's imaging plate and the well in its `src_filename`; fields named `FID<fid>`; `ensg_id` from the API, the path's kept as `ensg_id_in_path` |
| 4 | The API's records of how each image was made | Stored as attributes on each well and field, with the snapshot date in the root. The licence question to OpenCell covers them |
| 6 | Pixel size | 13 / 63 = 0.2063 µm in y and x, from the paper's optics; the TIFFs' 0.2 µm kept under each image's `tiff` attributes |

Calls 5, 7, 8 and 9 concern the tables and raw files of phase 2.

## Problems found in the source data

- **Ensembl IDs in the paths.** For 37 cell lines the bucket path's Ensembl ID
  differs from the one the API and the website's tables use. Looked up in
  Ensembl release 116, 30 are the same gene on an alternate haplotype or
  patch, 3 belong to other genes (NAPG's path names RAB11FIP5, LIG1's names
  LRIG1, SMN1's names SMN2), and 4 are IDs Ensembl no longer knows. The store
  keeps the path's value beside the API's. LAYOUTS.md lists all 37.
- **Pixel size.** Every TIFF states 0.2 µm per pixel. The paper's optics give
  0.2063 µm, 3% larger, and CZI's SubCell, built from the same bucket, uses
  0.206349. Confirming with the OpenCell team that no relay lens changes the
  magnification is part of the licence email.
- **Unlisted cell lines.** 86 cell lines with 390 fields of view are in the
  public bucket, but the website and its API do not list them. 85 are second
  lines of a gene that has a listed line, and one is HSP90B1, the only line
  of its gene.
- **Unnamed channels.** The TIFFs do not name their channels. The order,
  Hoechst 33342 then mNeonGreen, comes from the website's download page.
- **Imaging well and library well.** A cell line's `plate_id` and `well_id`
  in the API are where the line was made, not where it was imaged. The two
  wells agree for 4,062 of the 5,911 listed fields. The store places each
  field by its imaging plate and well.

## Upstream issues hit

[docs/upstream.md](../../docs/upstream.md) has the details of each.

- **Dropped reads.** A locally stored repo's virtual chunk reads inherited
  the local disk's single try, so one dropped connection failed a read
  ("connection closed before message completed") during the sample's pixel
  gate ([icechunk#2256](https://github.com/earth-mover/icechunk/issues/2256),
  [icechunk#1431](https://github.com/earth-mover/icechunk/issues/1431)).
  `minus80.core.icechunk_io` reads with 10 tries.
- **Serial commits.** Icechunk fetches manifests one at a time during a commit
  unless told otherwise, and an Arraylake repo's stored config keeps that
  default ([icechunk#2273](https://github.com/earth-mover/icechunk/issues/2273),
  closed by [#2274](https://github.com/earth-mover/icechunk/pull/2274), which
  kept the default of 1).
- **Query strings.** Icechunk drops a virtual chunk URL's query string on
  read ([icechunk#2444](https://github.com/earth-mover/icechunk/issues/2444)).
  It does not affect the images, but it blocks referencing the cytoself crops
  on Google Drive in phase 2.

## Phase 2

The tables beside the store have not been built. PLAN.md lists them under
"Steps, scope and risk". They add Iceberg tables without changing the store.
