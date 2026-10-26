# OpenCell: ingest plan

## Summary

The plan brings in everything OpenCell published, raw and processed.

- **Images.** A virtual OME-Zarr store of OpenCell's live-cell images in
  Icechunk, laid out as HCS plates: one plate per imaging plate, one well per
  cell line, one image per field of view. Every chunk references a strip in
  the public bucket `s3://czb-opencell`, so nothing is copied or decoded.
- **The API's records.** The plate, the well and each field's acquisition
  settings come from the OpenCell website's JSON API.
- **The other assays.** OpenCell's mass-spectrometry, sequencing and embedding
  deposits go beside the store as Iceberg tables. Raw files that no reader can
  reference in place are listed by URL, size and checksum rather than copied.

**Asked of the reviewer:** approve the layout and steps below, and decide the
nine judgement calls, each with a recommended default.

| # | Decision | Recommended |
|---|---|---|
| 1 | The 6,301 maximum projections | **Decided:** nested in each field image as `proj`, and repeated in `projections/` plates for browsing |
| 2 | The 86 cell lines the OpenCell website does not list (390 fields of view) | **Decided:** the `unlisted/` collection beside the plates |
| 3 | The hierarchy and keys | **Decided:** HCS plates; fields named `FID<fid>`; the API's Ensembl ID |
| 4 | The API's records of how each image was made | Store them as attributes, with the snapshot date |
| 5 | The study's results on the website and in the supplement, and the API's localization grades | Ask opencell@czbiohub.org for a licence first |
| 6 | The pixel size: 0.2 µm in the TIFFs, 0.2063 µm from the optics | **Decided:** 13 / 63 µm; the TIFF's value kept under `tiff` (LAYOUTS.md) |
| 7 | The 179 GB of Thermo raw files | List them by URL and checksum, beside MaxQuant's scan tables |
| 8 | The 5.6 TB of timsTOF raw files, deflated inside PRIDE's zips | List them now; ask PRIDE for uncompressed zips, then read frames in place with a new codec |
| 9 | SubCell's crops of the projections (another group's processing) | Link them from the root attributes |

Nothing is written to a shared resource before approval. The full local run,
its verification, and any Arraylake write each need approval in turn (see
"Steps, scope and risk").

## The archive

OpenCell ([Cho et al. 2022](https://doi.org/10.1126/science.abi6983)) tagged
1,310 human proteins at their own genes with split mNeonGreen2 in HEK293T
cells. The team imaged each tagged cell line alive by spinning-disk confocal
microscopy in 96-well plates, with nuclei stained by Hoechst 33342. They
measured each tagged protein's interactors by IP-MS (immunoprecipitation then
mass spectrometry), and the cells' protein and RNA abundance by whole-cell mass
spectrometry and RNA-seq.

**The bucket.** `s3://czb-opencell` (us-west-2, anonymous) holds 12,603
objects, 912 GB, licensed CC BY-SA 4.0 per the
[AWS Open Data registry](https://registry.opendata.aws/czb-opencell/):

- 6,301 `*_stack.tif`: ImageJ hyperstacks, ZCYX, uint16, 600 × 600, two
  channels, 51 to 131 slices, uncompressed, one strip per plane;
- 6,301 `*_proj.tif`: each stack's maximum over z, CYX;
- one stock `index.html`.

Paths are
`microscopy/raw/<GENE>_<ENSG>/OC-FOV_<GENE>_<ENSG>_CID<cid>_FID<fid>_{stack,proj}.tif`.
LAYOUTS.md has the depth table, the axes and every key mismatch.

**Everything else.** Surveyed 2026-10-23:

| source | raw | processed | key | licence |
|---|---|---|---|---|
| website JSON API | | 1,310 cell lines; 25,031 fields of view with imaging plate, well, settings, crop offset | CID, FID | none stated |
| website CSV downloads | | library metadata, localization, abundance, significant interactions | Ensembl ID | none stated |
| paper supplement ([BioStudies S-EPMC9119736](https://www.ebi.ac.uk/biostudies/studies/S-EPMC9119736)) | | Tables S1 to S9; Methods and figures PDFs | Ensembl ID or gene name | none stated |
| Figshare [16754965](https://doi.org/10.6084/m9.figshare.16754965), [16755205](https://doi.org/10.6084/m9.figshare.16755205) | | per-target cytoself embeddings (1,294 × 9,216, h5ad, 160 MB); every IP-MS bait-prey pair (CSV, 480 MB) | Ensembl ID | CC BY 4.0 |
| PRIDE [PXD024909](https://www.ebi.ac.uk/pride/archive/projects/PXD024909), IP-MS | 4,791 Bruker timsTOF runs in 5 zips, 5.6 TB | MaxQuant tables, 0.5 GB zipped; sample table | bait gene name, run name | CC0 |
| PRIDE [PXD029191](https://www.ebi.ac.uk/pride/archive/projects/PXD029191), whole proteome | 72 Thermo `.raw`, 179 GB | MaxQuant tables 19 GB; mzTab 1.5 GB; FASTA | UniProt ID; raw file and scan | CC0 |
| SRA [PRJNA772934](https://www.ncbi.nlm.nih.gov/bioproject/PRJNA772934) (GEO GSE186192), RNA-seq | 3 runs, about 39 M read pairs each | kallisto transcript table, 4.8 MB | Ensembl transcript ID | GEO's terms |
| SRA [PRJNA1026361](https://www.ncbi.nlm.nih.gov/bioproject/PRJNA1026361), genotyping amplicons | 271 runs from library plates P0001 to P0006 | | library plate and well | SRA's terms |
| cytoself crops, Google Drive | 1,134,592 crops, 100 × 100 × 4, float32, 182 GB in 10 `.npy` | 10 label CSVs, 65 MB | Ensembl ID, FID | none stated |
| GitHub czbiohub-sf/opencell-portal-pub, 2021-opencell-figures | | library plate map; figure inputs and clusterings | gene, plate, well | CZ Biohub non-commercial |

**How people use it.**

- The OpenCell website shows each protein's projections and stacks beside its
  localization labels and interactors.
- The paper's analyses start from the per-protein tables and the image
  embeddings.
- The deposits in PRIDE and SRA are what anyone re-analysing the mass spec or
  the RNA-seq starts from.

## Proposed layout

### The tree

```
/                                          annotations: title, citation, DOI, licence, source, channel source,
                                           pixel size and its source, API snapshot date, every deposit's accession
                                           wells: one row per well (path, gene, cell_line_id, ensg_id, fields)
plates/<plate>                             ome.plate: rows A-H, columns 1-12, wells holding images (176 plates)
plates/<plate>/<row>/<column>              ome.well: field images; annotations: gene, Ensembl ID, how the line was made
plates/<plate>/<row>/<column>/FID<fid>     image: the z-stack, array 0 (c, z, y, x); derived_images: {proj}
plates/.../FID<fid>/proj                   image: the projection, array 0 (c, y, x)
projections/<plate>/.../FID<fid>           the same plates holding only the projections, same bytes
unlisted/                                  bioformats2raw.layout collection of the 86 unlisted lines
unlisted/<GENE>/CID<cid>/FID<fid>[/proj]   stack and nested projection, with only the path's facts
```

AAMP's field FID 13888 is at `plates/PML0116/B/12/FID00013888`: an array of
shape (2, 51, 600, 600), chunks (1, 1, 600, 600), with its projection at
`plates/PML0116/B/12/FID00013888/proj`.

### Axes, units, levels

- **Stacks** are `(c, z, y, x)`, built from the stored ZCYX by
  `minus80.core.manifest.permute_dims`. One chunk is one plane, so no transpose
  codec is needed.
- **Projections** are `(c, y, x)` as stored.
- **Pixel size.** `y` and `x` are 13 / 63 = 0.2063 µm, from the Methods'
  optics: a 63× objective and an Andor iXon Ultra 888 camera with 13 µm
  pixels. The TIFFs state 0.2 µm, which is kept under `tiff` (decision 6;
  LAYOUTS.md records the source and the assumption). `z` is 0.5 or 0.2 µm,
  from each TIFF's ImageJ `spacing`. The API states the same z step for all
  5,911 published fields.
- **Units** are `micrometer`.
- **Channels.** Channel 0 is Hoechst 33342 and channel 1 mNeonGreen, from the
  [download page](https://opencell.sf.czbiohub.org/download). The TIFFs name
  neither. An `omero` block labels and colours them.
- **Levels.** Each image has one level, the TIFF's own.

### Referenced, written, beside

| product | how |
|---|---|
| stacks, projections, unlisted lines' images | referenced: virtual arrays over the TIFF strips |
| plate, well and image documents; per-field acquisition records; each line's guide, terminus, library well | written: JSON attributes |
| Figshare embeddings and IP-MS pairs; both PRIDE projects' MaxQuant tables; the kallisto table; the sample tables | Iceberg tables beside the store |
| Thermo `.raw`, timsTOF zips, mzTab, FASTA, RNA-seq and amplicon reads | Iceberg tables of files: URL at every host, size, checksum, and the sample each belongs to |
| site and supplement tables; the API's localization grades | Iceberg tables, after a licence (decision 5) |
| cytoself crops | virtual arrays over the Drive files if Icechunk can read them; otherwise a table of files |
| SubCell crops; BioImage Archive S-BIAD1287 | linked from the root attributes as related datasets |

The MaxQuant tables are wide, with one column per run in each of several
families; IP-MS's `proteinGroups.txt` has up to 8,850 columns. They are
written long, keyed by (protein group, run).

### Layouts considered and rejected

- **One image per file in a `bioformats2raw.layout` collection,
  `<GENE>/CID<cid>/FID<fid>`, for every line.** A gene's fields would sit
  together. But OME-Zarr has a structure for multi-well plate data, HCS (0.5
  §1.2), and this layout loses the plates and wells the data has.
- **Fields of view stacked into one array per depth, `(fov, c, z, y, x)`.** It
  would hold 8 arrays instead of 11,822, but it breaks a MUST: "The "axes" MUST
  contain 2 or 3 entries of "type:space" and MAY contain one additional entry
  of "type:time" and MAY contain one additional entry of "type:channel" or a
  null / custom type" (0.5 §2.4).
- **One array padded to 131 slices.** It mixes 0.5 µm and 0.2 µm z steps on
  one axis, and pads 5,000 stacks with chunks that don't exist.

## What has been tested

All runs read from a laptop in California, outside AWS.

- **Sample ingest**: five genes, chosen to cover the cases:
  - AAMP, with stacks at 0.5 µm and 0.2 µm;
  - ABCE1, with 131 slices;
  - AKAP13, with two lines, one unlisted;
  - BAG6, with a mismatched Ensembl ID;
  - HSP90B1, with no listed line.

  The run wrote 19 listed fields on 8 plates and 8 fields of 2 unlisted lines,
  73 images, in 2.1 s of parsing and 0.3 s of writing.
- **Verification of the sample**: every gate in `verify.py` passes on all 73
  images:
  - the shared gates (schema, dimension names, physical size, declared images,
    levels, provenance);
  - completeness, z step, pixel size and Ensembl ID;
  - the reference gate, every chunk against tifffile's strip index;
  - the sampled pixel gate, three fields decoded equal to tifffile, each
    projection equal to its stack's maximum over z.

  Two earlier pixel passes failed when an Icechunk virtual-chunk read dropped
  its connection ("connection closed before message completed"), once with a
  single reader. REPORT.md gives the cause and the fix.
- **The whole bucket**: all 12,602 files parse with `TiffParser` through the
  ingest's own code, with no refusals. Every file matches a header survey made
  with tifffile on shape, dtype, axes, z spacing and units. Every chunk
  reference lies inside its object, is non-empty and overlaps no other. 105
  planes from 21 stacks across every depth, and their projections, equal
  tifffile's decode. Parsing took a median of 1.08 s per stack under 32
  threads from a laptop, 1,056 s in all.
- **Phase 1 run** into the Arraylake repo `earthmover-biology/opencell`
  (Cloudflare R2), from a laptop in California, snapshot
  `XADEMEKMPVBQX6V4KWC0`:
  - 5,911 listed fields in 2,193 wells on 176 plates, and 390 fields of 86
    unlisted lines;
  - parsing 12,602 TIFFs took 360 s, writing 6,301 fields 67 s, and the commit
    115 s, 9 min 7 s in all.

  A first attempt stalled in the commit. An Arraylake repo's stored config
  keeps Icechunk's commit manifest concurrency at 1, so the commit wrote about
  18,500 arrays one round trip at a time. `minus80.core.icechunk_io.arraylake_repository`
  now opens the repo with the concurrency minus80 uses everywhere else, 32.
- **Commit size**: 12,602 images built from real parsed arrays wrote in 26 s
  and committed in 1.9 s to a local repo. The cost on S3 is unmeasured.
- **timsTOF frames**: once unzipped, a run's frames read back through Zarr's
  zstd and shuffle codecs on rectilinear chunks with Icechunk virtual
  references, equal to a reference decoder on 88 frames. Every member of
  PRIDE's zips is deflated, which blocks this in place.

## Judgement calls

1. **Projections.**
   - *Evidence:* each projection is exactly the maximum over z of its stack, so
     it is derivable. It is also what the OpenCell site leads with, at 1.4 MB
     against 73 to 189 MB for a stack.
   - *Cost:* referencing them adds 5,911 arrays.
   - *Recommended:* reference them, as `projections/`.
2. **Unpublished lines.**
   - *Evidence:* the API answers 404 for 86 lines, which hold 390 fields of
     view, so they have no plate or well. 85 are second lines of a published
     gene, and one is HSP90B1.
   - *Recommended:* a separate `bioformats2raw.layout` collection beside the
     plates. The spec does not let a collection list plates among its
     images, but sibling groups can be one each. Ask the team why the lines weren't published.
3. **Hierarchy and keys.**
   - *Evidence:* every published field has an imaging plate and a well, and
     every well holds one cell line.
   - *Recommended:* HCS plates, with fields named `FID<fid>`. Use the API's
     Ensembl ID as `ensg_id`, and keep the path's as `ensg_id_in_path`; the
     two differ for 37 lines. A root `wells` table finds a gene's wells.
4. **The API's records of how each image was made.**
   - *Evidence:* the plate, well, acquisition settings, crop offset, and each
     line's guide and terminus exist only in the API, which states no licence.
     The layout depends on the plate and well.
   - *Recommended:* store them as attributes, and include the API in the
     licence question.
   - *Note:* removing them later takes more than a commit. Icechunk keeps every
     earlier snapshot, so the old attributes stay readable until those
     snapshots are expired or the repo is rebuilt.
5. **The study's results.**
   - *Evidence:* six tables (localization, abundance, interactions, library
     metadata, S5, S9) and the API's localization grades state no licence. The
     images are CC BY-SA 4.0, whose ShareAlike term applies to adapted
     material; whether tables beside the store count is a reading to confirm.
   - *Recommended:* ask opencell@czbiohub.org first, then add them as Iceberg
     tables keyed on the API's Ensembl ID.
6. **Pixel size.**
   - *Evidence:* the TIFFs state 0.2 µm. The Methods give 13 µm ÷ 63 = 0.2063
     µm, and CZI's SubCell uses 0.206349.
   - *Recommended:* write 13 / 63 µm, keep 5/1 under `tiff`, record it in
     LAYOUTS.md, and ask the team to confirm that no relay lens sits between
     objective and camera.
7. **Thermo raw files.**
   - *Evidence:* every maintained reader wraps Thermo's closed RawFileReader,
     none exposes byte offsets, and the m/z values are computed from per-scan
     calibration.
   - *Recommended:* list them by URL and checksum, beside MaxQuant's
     `msScans` and `msmsScans` tables, which describe every scan. The
     alternative, conversion to mzML or Parquet, is a 179 GB copy to host.
8. **timsTOF raw files.**
   - *Evidence:* the frames suit Zarr. Each is a size, a scan count and one
     zstd stream of byte-shuffled 32-bit words, and a codec of about 150 lines
     would decode it into peaks. That would be about 55 M chunk references for
     all runs. But PRIDE's zips deflate every member, so no frame is
     addressable in place.
   - *Recommended:* list the runs now, and ask PRIDE or the submitters to
     re-zip them without compression (2% larger). Write the codec when they
     do. Until then, the only way to read frames in place is a hosted 5.7 TB
     copy.
9. **SubCell crops.**
   - *Evidence:* 94,426 PNG crops of the projections, CC BY-SA, made by CZI,
     not OpenCell.
   - *Recommended:* link them, with the BioImage Archive's re-imaging of
     OpenCell lines (S-BIAD1287), from the root attributes.

## Steps, scope and risk

**Phases.** Phase 1 is the images alone; the tables follow beside the store
without changing it.

**Phase 1, the images. Approved, into an Icechunk repo in the managed R2
bucket.**

1. The API snapshot of 2026-10-23 is the source of record. It was taken with
   about 1,400 GETs, one at a time.
2. Run `ingest.py` into the repo. It lists the bucket and places each field,
   then parses all 12,602 files with `TiffParser`. It writes the plates, wells
   and field images, the `projections/` plates and the `unlisted/` collection,
   and commits once, printing progress at a steady interval.
3. Run `verify.py`. It checks every chunk reference, read back from the store,
   against tifffile's strip index of its file, reading only each file's
   directories (about 0.8 GB in all). It decodes the pixels of one stack and
   projection per depth in each tree, and checks the pixel size on every
   image.
4. Open the store cold in a fresh shell, and in a plate viewer.
5. Write REPORT.md and a demo notebook that opens the store cold.

**Phase 2, the tables.** Each write is approved separately.

1. Build the Iceberg tables:
   - the Figshare tables;
   - the MaxQuant tables of both PRIDE projects, melted long;
   - the kallisto table and the sample tables;
   - the tables of raw files, from PRIDE's, SRA's and ENA's file reports with
     their checksums.
2. Test whether Icechunk reads the cytoself `.npy` files over Drive URLs.
   Icechunk drops a URL's query string on read (earth-mover/icechunk#2444),
   and Drive's download URLs are all query string, so expect a table of files
   until that is fixed.
3. Check every table's row count against its source.

**In scope:** everything in the source table above, raw and processed. The
site's and supplement's results are in scope too, but wait for a licence.

**Not a goal:**
- recomputing anything OpenCell computed, such as the site's resampled
  stacks, the cytoself embeddings or the Perseus abundance step;
- converting raw spectra or reads into other formats.

**Writes that need approval:**
- the full run into a repo, even a local one;
- any table written beside it;
- any write to Arraylake or a shared bucket;
- any request to PRIDE or the OpenCell team.

**Risks.**

- **Commit cost.** The commit's cost on S3 for 11,822 or more arrays is
  unmeasured. Measure it on an own-prefix bucket before the Arraylake write.
- **The API.** It could change or go away. The snapshot is the source of
  record, and its date is in the store.
- **Dropped reads.** Reads through Icechunk dropped a connection during the
  sample's pixel gate. If Icechunk doesn't retry such reads, readers of the
  published store will hit the same error.
- **Slow and fragile hosts.**
  - PRIDE's HTTPS server is slow and erratic: one 200-byte range took 28 s.
  - Google Drive is not an object store.

  Readers of the tables of files depend on both.
- **Licence.** The store carries CC BY-SA 4.0. The API and the site's tables
  state none, and the library plate map on GitHub is non-commercial.
