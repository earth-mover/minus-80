# Cell Painting Gallery: ingest plan

## Summary

The plan brings eleven CPJUMP1 plates from the Cell Painting Gallery into
`earthmover-biology`, with tables that say what was done to every well.

- **Images.** A virtual OME-Zarr store of eleven plates in Icechunk, laid out
  as OME-Zarr 0.5 HCS plates under the gallery's own path. Each field's full
  resolution references the gallery's LZW TIFFs in place, one chunk per strip;
  a written 16× overview level makes a whole plate viewable.
  - Nine plates are already in `earthmover-public/cellpainting-gallery`. Their
    references and overviews are copied from there, and their metadata is
    written afresh.
  - Two plates are new: BR00117022 and BR00117023, ORF overexpression in U2OS
    at 48 h. They are ingested with `minus80.parsers.tiff`.
- **Tables.** Three Iceberg tables beside the store, at plate, well and image
  grain. The well table flags every control from JUMP-Target's metadata. They
  replace the earlier `harmonized_metadata`, which repeats each well per site,
  channel and annotation variant and has no control flag.

**Asked of the reviewer:** approve the layout and steps below, and decide the
judgement calls, each with a recommended default.

| # | Decision | Recommended |
|---|---|---|
| 1 | Location, org, scope, HCS layout, rebuilding the tables, the control source | **Decided** (2026-10-26) |
| 2 | The nine existing plates | Copy their references and overviews from `earthmover-public`; rewrite their metadata |
| 3 | Opera Phenix LZW strips that decode past the image (157 on the nine plates) | Shorten each reference at ingest, in `minus80.parsers.tiff`; record the codec fix it waits on |
| 4 | The 16× overview level | Keep it; build it for the two new plates in one decode pass |
| 5 | 47 sites on three plates that are in the bucket but not in `load_data.csv` | Leave them out, and list them in the plate's attributes |
| 6 | Field names | The gallery's site number, `1` to `9` (or `16`) |
| 7 | The 64 DMSO wells per compound plate, which no key joins to a control flag | Flag them `negcon` by a stated rule |
| 8 | The four wells with no perturbation on each CRISPR and ORF plate | Leave their perturbation and control flag blank |
| 9 | The illumination functions | Reference the `.npy` files in place, one image per plate |
| 10 | The image table's grain | One row per field, one URL column per channel |
| 11 | BR00117022 and BR00117023's later re-imaging batches | Out of scope |

Nothing is written to a shared resource before approval. Creating the repo,
the full run and each table write need approval in turn (see "Steps, scope and
risk").

## The archive

The Cell Painting Gallery ([Weisbart et al.
2024](https://doi.org/10.1038/s41592-024-02399-z)) is Broad's public bucket
`s3://cellpainting-gallery` (us-east-1, anonymous, CC0 1.0). CPJUMP1 is
dataset `cpg0000-jump-pilot`, source `source_4`, batch `2020_11_04_CPJUMP1`
([Chandrasekaran et al. 2024](https://doi.org/10.1038/s41592-024-02241-6)): one
384-well layout of compounds, one of CRISPR guides and one of ORFs, each
targeting the same genes, imaged in A549 and U2OS cells at several time points
on a Revvity Opera Phenix.

**The plates in scope.** Surveyed 2026-10-26. Cell type, time and modality
come from the paper's `experiment-metadata.tsv`
([jump-cellpainting/2024_Chandrasekaran_NatureMethods](https://github.com/jump-cellpainting/2024_Chandrasekaran_NatureMethods/blob/56845c7d4dc322652952783d91dae0ffef47829f/benchmark/output/experiment-metadata.tsv));
the platemap comes from the batch's `barcode_platemap.csv`.

| plate | modality | cells | time (h) | sites per well | TIFFs | GB | sites in `load_data.csv` | in `earthmover-public` |
|---|---|---|---|---|---|---|---|---|
| BR00116991 | compound | A549 | 24 | 9 | 27,648 | 67.3 | 3,456 | yes |
| BR00116992 | compound | A549 | 24 | 9 | 27,648 | 67.7 | 3,455 | yes |
| BR00116993 | compound | A549 | 24 | 9 | 27,648 | 67.0 | 3,419 | yes |
| BR00116994 | compound | A549 | 24 | 9 | 27,648 | 67.4 | 3,447 | yes |
| BR00116995 | compound | U2OS | 24 | 9 | 27,648 | 61.8 | 3,456 | yes |
| BR00116996 | CRISPR | U2OS | 144 | 9 | 27,648 | 64.9 | 3,456 | yes |
| BR00117006 | ORF | A549 | 96 | 9 | 27,648 | 69.0 | 3,456 | yes |
| BR00117016 | compound | A549 | 48 | 16 | 49,152 | 115.8 | 6,144 | yes |
| BR00117024 | compound | U2OS | 24 | 9 | 27,648 | 64.7 | 3,456 | yes |
| **BR00117022** | ORF | U2OS | 48 | 9 | 27,648 | 67.9 | 3,456 | no |
| **BR00117023** | ORF | U2OS | 48 | 9 | 27,648 | 67.2 | 3,456 | no |
| total | | | | | 325,632 | 780.8 | | |

The two new plates come from the same source and batch as the other nine,
`cpg0000-jump-pilot/source_4/2020_11_04_CPJUMP1`, not from `cpg0016-jump`.
The earlier ingest built and verified this source's layout on all nine plates,
so the new plates use a tested layout.

**One plate in the bucket.**

- `images/<batch>/images/<plate>__<time>-Measurement<n>/Images/`:
  - one TIFF per site and channel, `r<row>c<col>f<site>p01-ch<n>sk1fk1fl1.tiff`;
  - 8 channels per site: five stains and three brightfield planes;
  - each TIFF is uint16, 1080 × 1080, one LZW strip;
  - beside the TIFFs sit the Harmony `Index.idx.xml`, an assay layout XML and
    a flat-field profile XML.
- `images/<batch>/illum/<plate>/<plate>_Illum<channel>.npy`: CellProfiler's
  illumination function per channel, float32, 1080 × 1080, a 128-byte NumPy
  header and raw little-endian bytes.
- `workspace/load_data_csv/<batch>/<plate>/load_data.csv`: one row per (well,
  site), one `URL_Orig<channel>` column per channel, and the acquisition
  metadata (position, time, pixel size).
- `workspace/profiles/<batch>/<plate>/`: well-level profiles at seven
  pycytominer stages, gzipped CSV.
- `workspace/backend/<batch>/<plate>/<plate>.sqlite`: per-cell CellProfiler
  features, 16 to 62 GB per plate.

**Perturbations and controls.** JUMP-Target
([jump-cellpainting/JUMP-Target](https://github.com/jump-cellpainting/JUMP-Target/tree/2da0551610a26548a384537b28d05f6fc72ea33c),
MIT, commit `2da0551`, the head of `master` on 2026-10-26) publishes one
platemap and one metadata table per modality. The platemaps are identical, well
for well, to the copies in the gallery bucket, and the metadata's control flags
agree with the bucket's `external_metadata/` on every sample. Joined, each
layout holds:

| layout | `trt` | `negcon` | `poscon_cp` | `poscon_diverse` | `poscon_orf` | no sample |
|---|---|---|---|---|---|---|
| compound | 260 | 64 (DMSO, by rule; decision 7) | 26 | 28 | 6 | 0 |
| CRISPR | 260 | 60 | 26 | 28 | 6 | 4 |
| ORF | 260 | 60 | 26 | 28 | 6 | 4 |

**How people use it.**

- The paper benchmarks matching a compound to the gene it targets, from
  well-level profiles.
- JUMP's `jump_portrait` downloads TIFFs for chosen perturbations.
- Users of the earlier store query Broad's per-image table and read fields
  from the Icechunk repo.

## Proposed layout

### Spec rules that constrain it

From OME-Zarr 0.5:

- §1.2: "Three groups MUST be defined above the images: the group above the
  images defines the well and MUST implement the well specification ... the
  group above the well defines a row of wells ... the group above the well row
  defines an entire plate ... It MUST implement the plate specification."
- §1.2: "A well group SHOULD NOT be present if there are no images in the
  well." BR00116993's well N01 has no site in `load_data.csv`, so that plate
  holds 383 well groups.
- §2.7: "Each row in the physical plate MUST be defined, even if no wells in
  the row are defined", and the same for columns. Every plate declares rows A
  to P and columns 1 to 24.
- §2.8: each field's `path` "MUST contain only alphanumeric characters". Site
  numbers qualify (decision 6).
- §2.1: "The "axes" MUST contain 2 or 3 entries of "type:space" and MAY contain
  one additional entry of "type:time" and MAY contain one additional entry of
  "type:channel" or a null / custom type." This rules out a well or site axis
  beside the channel axis.
- §2.2: "It is not possible to mix collections of images with plates at
  present." The illumination images therefore sit beside the plates as
  standalone images, not in a collection with them.

### The tree

```
/                                         title, licence (CC0 1.0), citations, source bucket, codecs a reader needs,
                                          JUMP-Target and experiment-metadata commits
cpg0000-jump-pilot/                       title, description, citation (Chandrasekaran et al. 2024)
cpg0000-jump-pilot/source_4/              microscope: Revvity Opera Phenix
.../2020_11_04_CPJUMP1/                   the batch: source prefix, CellProfiler illumination pipeline text
.../2020_11_04_CPJUMP1/<plate>            ome.plate: rows A-P, columns 1-24, wells with images, field_count,
                                          one acquisition (load_data's first AbsTime); modality, cells, time,
                                          platemap name, sites left out (decision 5), LZW-shortened chunks (decision 3)
.../<plate>/<row>/<column>                ome.well: its fields; broad_sample, perturbation, gene or compound,
                                          pert_type, control_type
.../<plate>/<row>/<column>/<site>         image: level 0 (c, y, x) referenced; level 1 written; the 8 source URLs
.../2020_11_04_CPJUMP1/illum/<plate>      image: (c, y, x) float32, the 8 illumination functions, referenced
```

BR00116991's well K11, site 1, is at
`cpg0000-jump-pilot/source_4/2020_11_04_CPJUMP1/BR00116991/K/11/1`:
level 0 has shape (8, 1080, 1080) and chunks (1, 1080, 1080); level 1 has shape
(8, 67, 67).

### Axes, units, levels

- **Fields** are `(c, y, x)`. Each channel is one TIFF of one strip, so each
  level-0 chunk is one whole TIFF strip.
- **Channels** are in protocol order: DNA, RNA, ER, AGP, Mito, Brightfield,
  HighZBF, LowZBF. The order comes from a fixed list, never from sorting the
  names. Every `load_data.csv` in scope has the same eight `URL_Orig` columns.
  An `omero` block labels and colours them.
- **Pixel size.** `y` and `x` are 0.5979760809567617 µm, from `load_data.csv`'s
  `Metadata_ImageResolutionX/Y` (5.98e-7 m), identical on all eleven plates and
  equal to JUMP's `microscope_config.csv` value for Opera Phenix sources
  (0.597976). The TIFFs' resolution tags state 6.45 µm, the camera's pixel
  pitch, which `TiffParser` reports as the scale. The ingest overrides it and
  keeps the tag under `tiff` in the field's attributes.
- **Position.** Each field's `translation` places it in its well, from
  `Metadata_PositionX/Y` with Y negated. The earlier ingest checked that sign
  by edge continuity between neighbouring fields on BR00117016.
- **Units** are `micrometer`.
- **Levels.** Level 0 is full resolution. Level 1 is a 16× block mean, 67 × 67,
  zstd-compressed, so a plate viewer can draw all 3,456 fields.

### Referenced, written, beside

| product | how |
|---|---|
| level 0 of every field | referenced: one chunk per TIFF strip; nine plates copied from `earthmover-public`, two parsed with `TiffParser` |
| level 1 of every field | written: copied for nine plates, built for two |
| illumination functions | referenced: one chunk per `.npy`, offset 128, 4,665,600 bytes |
| plate, well and field documents; each well's perturbation and control flag; source URLs | written: JSON attributes |
| plate, well and image tables | Iceberg tables beside the store |

The store drops the earlier store's non-spec nodes:

- each plate's `cube` (a 5-D `(well, site, c, y, x)` copy of the references);
- its `annotations` and `profiles` arrays;
- the batch's `plates` arrays.

The annotations move into well attributes and the tables. The profiles wait
for a later phase.

### The tables

**How `harmonized_metadata` ended up as it is.** The earlier ingest registered
Broad's harmonized per-image draft for the pilot unchanged, to keep Broad's
schema as the contract instead of publishing a derived table of its own. That
table is one row per TIFF and per annotation variant: 4.31 M rows cover 2.72 M
TIFFs on the 75 pilot plates, because a compound with two target genes gets two
rows. Its `Perturbation_Category` names the modality, and no column flags a
control. A plate table and a well table were designed beside it but never
built.

The rebuilt tables, in one Iceberg namespace beside the repo, each with its
grain and key:

| table | grain and key | built from | rows |
|---|---|---|---|
| `plates` | plate: (`dataset`, `source`, `batch`, `plate`) | `barcode_platemap.csv`, `experiment-metadata.tsv`, `load_data.csv` | 11 |
| `wells` | well: plate key + `well` | JUMP-Target platemap and metadata | 4,224 |
| `images` | field: well key + `site` | `load_data.csv` and the store | 40,657 |

- **`plates`**: platemap name, modality, cell type, time, cell line (parental or
  Cas9), the paper's anomaly note, microscope, pixel size, sites per well, wells
  and fields in the store, the plate's store path, and the repo and snapshot.
- **`wells`**: `broad_sample`, compound name, InChIKey and SMILES or gene,
  target genes, `pert_type` (`trt` or `control`), `control_type` (`negcon`,
  `poscon_cp`, `poscon_diverse`, `poscon_orf`), a boolean `is_control`, solvent,
  and the well's store path, blank for BR00116993 N01, which has no images.
- **`images`**: the field's store path, its position in micrometres, its
  acquisition time, and `url_DNA` to `url_LowZBF`, the eight source TIFFs.

The image row count is the sum of `load_data.csv` rows. Keys are strings and
integers, never floats.

### Layouts considered and rejected

- **One 5-D array per plate, `(well, site, c, y, x)`**, as the earlier store's
  `cube`. It breaks the §2.1 MUST above, since well and site would both be
  custom axes beside the channel.
- **Sites stacked per well, `(site, c, y, x)`.** It breaks the same rule.
- **A `bioformats2raw.layout` collection of fields.** It loses the plates and
  wells the data has, and HCS (§1.2) exists for this data.
- **Fields stitched into one image per well.** Positions are stage
  coordinates with gaps between fields, so stitching would invent pixels.

## What has been tested

All runs read from a laptop in California, outside AWS.

- **Survey**: every plate folder in scope listed with `aws s3 ls
  --no-sign-request`; the counts are in the table above. Each plate holds 384
  × sites × 8 TIFFs and three instrument files.
- **Headers**: one TIFF from each of four plates, covering a new plate, the
  16-site plate and a brightfield channel, parsed with `TiffParser` in 0.4 to
  0.8 s each. All are uint16, 1080 × 1080, one LZW strip, one chunk; the
  codec chain is `bytes` then `imagecodecs_lzw`.
- **LZW over-run**: two known Opera Phenix strips decoded with imagecodecs'
  unbounded `lzw_decode`, which is what the `imagecodecs_lzw` Zarr codec calls.
  BR00116991 `r02c03f04` ch4 decodes to 2,332,803 bytes against 2,332,800;
  BR00116994 `r07c19f05` ch2 raises `IMCD_LZW_CORRUPT`. Both decode correctly
  through tifffile, which bounds the output.
- **The earlier store**, read-only: the nine plates list 157 LZW-shortened
  chunks in their attributes, between 12 and 28 per plate. Their plate,
  well and field attributes declare OME-Zarr 0.5.
- **Sites left out of `load_data.csv`**: two of the 47 decoded with tifffile,
  BR00116993 J01 site 1 and BR00116992 C20 site 6. Both are all zeros; a listed
  neighbour in each well is not.
- **Controls**: JUMP-Target's platemaps and metadata joined at the pinned
  commit, with the counts above. Every non-blank sample in each platemap
  matches a metadata row.

Not yet run: any part of the ingest, a commit of the expected 81,000 arrays,
or a copy out of `earthmover-public`.

## Judgement calls

2. **The nine existing plates.**
   - *Evidence:* their level-0 references were verified against tifffile when
     they were written, including the 157 shortened LZW chunks. Their
     overviews took a full decode of 646 GB to build. Re-ingesting them would
     repeat that decode.
   - *Recommended:* read each plate's level 0 and level 1 out of
     `earthmover-public/cellpainting-gallery` with VirtualiZarr's
     `IcechunkParser`, write them under the new names, and write every
     attribute afresh from the sources. Level 1 is about 23 MB per plate.
   - *Cost:* the store then depends on a snapshot of another repo for its
     provenance. The plate attributes name that repo and snapshot.
3. **LZW strips that decode past the image.**
   - *Evidence:* on about 0.06% of strips, Harmony writes the end-of-information
     code at the previous code width, so an unbounded LZW decoder runs two or
     three bytes long or rejects the stream. `TiffParser` references the whole
     strip, and imagecodecs-zarr's `Lzw` codec decodes without a size bound,
     so a plain `TiffParser` ingest would leave those chunks unreadable. No
     imagecodecs issue covers it.
   - *Recommended:* in the one decode that builds the overview, check each
     strip; when the unbounded decode is wrong, reference the strip minus its
     last one to four bytes, accepted only if that decodes byte-identical to
     tifffile. The function moves into `minus80.parsers.tiff`, where any
     Opera Phenix ingest gets it. Each plate lists its shortened chunks. The
     upstream fix, a size-bounded decode in imagecodecs-zarr's `Lzw`, goes in
     `docs/upstream.md`.
4. **The overview level.**
   - *Evidence:* a plate view of 3,456 full-resolution fields reads 65 GB. The
     overview reads 23 MB. Building it decodes every pixel once, about 7
     minutes per plate in-region in the earlier ingest.
   - *Recommended:* keep it, and build it for the two new plates in the same
     pass as the LZW check.
5. **Sites in the bucket but not in `load_data.csv`.**
   - *Evidence:* 47 sites: 1 on BR00116992, 37 on BR00116993 (all of N01 and
     parts of column 1), 9 on BR00116994. Their TIFFs exist, the two decoded are
     all zeros, and CellProfiler measured none of them.
   - *Recommended:* follow `load_data.csv`, list the 47 sites and their URLs
     in the plate's attributes as present but not acquired, and record the
     zero check in LAYOUTS.md.
6. **Field names.**
   - *Evidence:* the earlier store numbered fields 0, 1, … in sorted site
     order, so on BR00116993 J01 field `0` is site 3.
   - *Recommended:* name each field by its gallery site number. A path then
     means the same site on every plate, and the tables join on it directly.
7. **DMSO wells.**
   - *Evidence:* the compound platemap leaves `broad_sample` blank on its 64
     DMSO wells and gives `solvent` DMSO. JUMP-Target's DMSO row, the only
     compound `negcon`, has `broad_sample` `NA`. An exact-key join flags none of
     them.
   - *Recommended:* flag a well `negcon`, DMSO, when it is on a compound
     platemap with a blank `broad_sample` and `solvent` DMSO. State the rule in
     the table's description and LAYOUTS.md. The earlier store's annotations
     applied the same rule.
8. **Wells with no perturbation on CRISPR and ORF plates.**
   - *Evidence:* E01, G04, J05 and L08 have no `broad_sample` in either
     platemap, and they were imaged.
   - *Recommended:* leave their perturbation and `control_type` blank, and
     `is_control` null.
9. **Illumination functions.**
   - *Evidence:* each `.npy` is a 128-byte header then raw `<f4` bytes in C
     order, so one chunk references it. The earlier store copied them. The
     spec names no illumination node inside a plate.
   - *Recommended:* one standalone image per plate at
     `<batch>/illum/<plate>`, `(c, y, x)` in the plate's channel order,
     referenced in place, with the CellProfiler pipeline text on the batch.
10. **The image table's grain.**
    - *Evidence:* JUMP's `jump_index.parquet` and `load_data.csv` are one row
      per site with a URL column per channel. Broad's harmonized table is one
      row per TIFF.
    - *Recommended:* one row per field, which is one OME image. A per-TIFF
      join unpivots the URL columns.
11. **Later re-imaging of the two ORF plates.**
    - *Evidence:* BR00117022 and BR00117023 were imaged again in four batches
      (Day 1, Day 4, Week 2, Week 4). Each is another acquisition of the same
      plate.
    - *Recommended:* out of scope. If added later, they become further
      acquisitions of the same plate groups.

## Steps, scope and risk

**Phase 0, code.** Write `ingests/cellpainting-gallery/`: `ingest.py`,
`verify.py`, `LAYOUTS.md` (ported from the earlier ingest's source notes) and a
notebook that opens the store cold. Add the LZW length check to
`minus80.parsers.tiff`, with a test on a real shortened strip.

**Phase 1, trial.** Into a local repo: BR00116991 copied and BR00117022
ingested. Run `verify.py`, open both plates in a plate viewer and time the
commit.

**Phase 2, the images. Needs approval to create
`earthmover-biology/cellpainting-gallery` and to write to it.**

1. Copy the nine plates out of `earthmover-public`, then commit.
2. Ingest the two new plates on Coiled VMs in us-east-1, one per plate:
   `TiffParser` over 55,296 TIFFs, then one decode pass that builds the
   overview and checks every strip. The earlier ingest indexed a 9-site plate
   in about 190 s on one `m6i.2xlarge`. Progress prints at a steady interval.
   Fork one session per plate and merge once.
3. Run `verify.py`, in about a minute:
   - metadata gates on every field: schema, axes, pixel size, provenance, and
     completeness against `load_data.csv`;
   - references against `TiffParser` and decoded pixels against tifffile on
     one field per plate and every shortened chunk of one plate.
4. Open the store cold in a fresh shell and in a plate viewer. Write REPORT.md.

**Phase 3, the tables. Each write is approved separately.** Build `plates`,
`wells` and `images`, check each row count against its source (11; 4,224;
40,657), check `control_type` counts against the JUMP-Target table above, and
pin each row to the repo and snapshot it describes.

**In scope:** the eleven plates' images and illumination functions, and the
three tables.

**Not a goal:**
- well-level profiles and per-cell features; the earlier `profiles__…` and
  `cells__…` tables stay in `earthmover-public` until a later plan;
- `cpg0016-jump` plates. When they come, their controls are in JUMP's
  `perturbation_control.csv`; `crispr.csv.gz` has no `Metadata_pert_type`
  column, and `orf.csv.gz` has one.

**Writes that need approval:**
- creating the repo and the Iceberg namespace in `earthmover-biology`;
- the full run into the repo;
- each table write;
- the Coiled run for the two new plates.

**Risks.**

- **Commit cost.** About 81,000 arrays: two per field, 3,456 fields per 9-site
  plate and 6,144 on BR00117016. OpenCell committed 18,500 arrays in 115 s;
  this commit is unmeasured. Phase 1 times it on the real object store.
- **Snapshot size.** About 10,800 nodes per 9-site plate, about 125,000 for
  eleven, against Icechunk's default cache of 500,000.
- **Readers need imagecodecs-zarr** for level 0, since `imagecodecs_lzw` is
  outside the Zarr core. The root attributes say so; level 1 uses only zstd.
- **An LZW strip no cut fixes.** The overview pass checks every strip of the
  new plates, but a strip whose shortened forms all decode wrong keeps its
  full reference and fails to read. None was seen on the nine plates; each
  plate lists any it finds.
- **Changing sources.** If Broad rewrites a `load_data.csv` or JUMP-Target
  changes a flag, the store and tables keep what was read. The JUMP-Target and
  experiment-metadata commits and the survey date are in the root attributes.
