# Cell Painting Gallery, JUMP (`cpg0016-jump`): ingest plan

## Summary

The plan brings the JUMP Cell Painting dataset, `cpg0016-jump` in the Cell
Painting Gallery, into `earthmover-biology` as virtual OME-Zarr HCS plates in
Icechunk, with tables that say what was done to every well.

- **Images.** Each plate becomes one OME-Zarr 0.5 HCS plate under the gallery's
  own path. Each field's full resolution references the gallery's TIFFs in
  place, one chunk per TIFF strip. A written 16× overview level makes a whole
  plate viewable.
- **Scope.** 2,298 plates from 12 of the 13 imaging sources, about 7.7 million
  fields and 49 million TIFFs. Source_6 is left out because its strips do not
  divide the image (decision 3).
- **Tables.** Three Iceberg tables beside the stores, at plate, well and field
  grain, built from JUMP's own metadata and each plate's `load_data.csv`.

**Asked of the reviewer:** approve the layout and steps below, and decide the
judgement calls. Decision 1 is the one most likely to draw an objection.

| # | Decision | Recommended |
|---|---|---|
| 1 | Where one repo ends | **One repo per batch**, 136 repos; merge later once Icechunk serves large snapshots |
| 2 | Which plates | Every plate in the bucket with a `load_data.csv`, source_6 excepted, including source_15's 21 plates JUMP's plate table omits |
| 3 | Source_6, strips of 6 rows over 970 | Leave it out until zarr-python reads rectilinear chunk grids by default |
| 4 | Source_8, 256 strips per TIFF | Keep one chunk per strip, 955 million references |
| 5 | Pixel size | `load_data.csv` where it states one, else JUMP's `microscope_config.csv`, never the TIFF tags |
| 6 | Positions and times missing from `load_data.csv` on 7 sources | Declare them not recorded; instrument files are a later task |
| 7 | Channels that differ between sources | Each plate's own channels in protocol order, no padding |
| 8 | Field names | The source's own site number, including sources 7 and 13, which count from 0 |
| 9 | Wells and sites the bucket holds but `load_data.csv` omits | Follow `load_data.csv` and list the rest in the plate's attributes |
| 10 | Perturbations and controls | JUMP's `well.csv.gz` chain at a pinned commit, exact-key joins |
| 11 | Layers left for later | Profiles, per-cell features, embeddings and instrument metadata |

Carried over from the pilot plan's approved decisions (2026-11-10): LZW strips
that decode past the image are shortened at ingest in `minus80.parsers.tiff`;
the 16× overview is built in the same decode pass; illumination functions are
referenced in place as one standalone image per plate; the field table has one
row per field.

Nothing is written to a shared resource before approval. Creating repos, each
Coiled run and each table write need approval in turn (see "Steps, scope and
risk").

## The archive

The Cell Painting Gallery ([Weisbart et al.
2024](https://doi.org/10.1038/s41592-024-02399-z)) is Broad's public bucket
`s3://cellpainting-gallery` (us-east-1, anonymous, CC0 1.0). `cpg0016-jump`
([Chandrasekaran et al.
2023](https://doi.org/10.1101/2023.03.23.534023)) profiles more than 116,000
compounds and 15,000 genes, by CRISPR knockout and ORF overexpression, in U2OS
cells. The gallery lists it at 358.4 TB, of which the images are more than
126 TB.

**Sources.** Thirteen sites imaged the plates on five kinds of microscope. Plate
counts are from listing `workspace/load_data_csv/` on 2026-11-10; image facts
are from one plate per source, probed 2026-10-08.

| source | microscope | batches | plates | wells | sites | image | channels |
|---|---|---|---|---|---|---|---|
| 1 | Opera Phenix | 6 | 55 | 1,472 of 1536 | 4 | LZW 1080², 1 strip | 5 stains + 1 brightfield |
| 2 | CV8000 | 13 | 229 | 384 | 6 | raw 996², 1 strip | 5 + 3 |
| 3 | Opera Phenix | 13 | 303 | 384 | 9 | LZW 1080², 1 strip | 5 |
| 4 | Opera Phenix | 13 | 277 | 384 | 9 | LZW 1080², 1 strip | 5 + 3 |
| 5 | CV8000 | 33 | 260 | 384 | 9 | raw 998², 1 strip | 5 + 3 |
| 6 | CV8000 | 13 | 246 | 384 | 9 | raw 970², **162 strips of 6 rows** | 5 + 3 |
| 7 | CV7000 | 8 | 128 | 384 | 9, numbered 0–8 | raw 1080×1280, 1 strip | 5 |
| 8 | ImageXpress Micro Confocal | 4 | 216 | 384 | 9 | raw 1024², **256 strips of 4 rows** | 5 |
| 9 | Opera Phenix | 11 | 108 | 1,248 of 1536 | 4 | LZW 1080², 1 strip | 5 |
| 10 | CV8000 | 18 | 222 | 384 | 6 | raw 1000², 1 strip | 5 + 3 |
| 11 | Operetta | 5 | 180 | 384 | 9 | LZW 1080², 1 strip | 5 + 1 |
| 13 | CV7000 | 6 | 154 | 384 | 9, numbered 0–8 | raw 1080×1280, 1 strip | 5 |
| 15 | Opera Phenix | 6 | 166 | 384 | 9 | LZW 1080², 1 strip | 4 (no RNA) + 3 |
| total | | 149 | 2,544 | | | | |

Wells and sites are those of the probed plate. Field and TIFF counts in this
plan multiply them by each source's plate count, so they are estimates until
every `load_data.csv` is read: about 7.7 million fields and 49 million TIFFs
without source_6. The gallery's own figure is "over 8 million images", and
adding source_6's 850,000 fields gives 8.6 million.

**One plate in the bucket.** For a plate `<plate>` of batch `<batch>` under
`cpg0016-jump/<source>/`:

- `images/<batch>/images/<plate folder>/`: one TIFF per site and channel, every
  one uint16 and single-page. The folder name is not the plate name everywhere
  (`<plate>__<time>-Measurement<n>` on Opera sources, `P01_<plate>` on
  source_5), and folders also hold thumbnails (source_8), calibration TIFFs (CV
  sources) and illumination TIFFs (source_13).
- `images/<batch>/illum/<plate>/<plate>_Illum<channel>.npy`: CellProfiler's
  illumination function per channel, a 128-byte NumPy header then raw bytes.
- `workspace/load_data_csv/<batch>/<plate>/load_data.csv`: one row per (well,
  site) and one `URL_Orig<channel>` column per channel. Acquisition metadata
  (stage position, time, pixel size) is present only on sources 1, 4, 9, 11
  and 15.
- `workspace/profiles/<batch>/<plate>/<plate>.parquet`: well-level profiles.
- `workspace/backend/<batch>/<plate>/<plate>.sqlite`: per-cell features.

**Perturbations.** The gallery holds no platemaps for `cpg0016-jump`.
[jump-cellpainting/datasets](https://github.com/jump-cellpainting/datasets)
`metadata/` gives each well's JCP2022 identifier (`well.csv.gz`), resolves it
through `compound.csv.gz`, `orf.csv.gz` and `crispr.csv.gz`, flags controls
in `perturbation_control.csv`, and gives each plate's type (`plate.csv.gz`)
and each source's pixel size (`microscope_config.csv`). `plate.csv.gz` has
2,525 plates: 1,876 compound, 251 ORF, 148 CRISPR, 141 TARGET2, 77 DMSO, 20
COMPOUND_EMPTY, 8 POSCON8 and 4 TARGET1.

**How people use it.**

- JUMP's own analyses start from the well-level profiles and the per-cell
  features, joined to the perturbation tables.
- [JUMP Hub](https://broad.io/jump) tools such as `jump_portrait` fetch the
  TIFFs of chosen perturbations.
- The earlier Earthmover store, `earthmover-public/cellpainting-gallery`, holds
  one cpg0016 plate each from sources 1, 2, 3, 4, 8, 10 and 11. Its readers
  select wells by perturbation and read fields from Icechunk.

## Proposed layout

### Spec rules that constrain it

From OME-Zarr 0.5:

- §1.2: "Three groups MUST be defined above the images: the group above the
  images defines the well and MUST implement the well specification ... the
  group above the well defines a row of wells ... the group above the well row
  defines an entire plate ... It MUST implement the plate specification."
- §1.2: "A well group SHOULD NOT be present if there are no images in the
  well."
- §2.7: "Each row in the physical plate MUST be defined, even if no wells in
  the row are defined", and the same for columns. A 1536-well plate declares
  rows A to AF and columns 1 to 48 even where `load_data.csv` lists fewer.
- §2.8: each field's `path` "MUST contain only alphanumeric characters".
- §2.1: "The "axes" MUST contain 2 or 3 entries of "type:space" and MAY contain
  one additional entry of "type:time" and MAY contain one additional entry of
  "type:channel" or a null / custom type." A well or site axis beside the
  channel axis is ruled out.
- §2.2: "It is not possible to mix collections of images with plates at
  present." The illumination images sit beside the plates as standalone images.

### The tree

One repo per batch (decision 1). Every repo keeps the gallery's path from the
dataset down, so a plate's path is the same whichever repo holds it:

```
/                                         title, license (CC0 1.0), citations, source bucket, codecs a reader needs,
                                          jump-cellpainting/datasets commit
cpg0016-jump/                             title, description, citation (Chandrasekaran et al. 2023)
cpg0016-jump/<source>/                    microscope, pixel size and where it was stated
.../<source>/<batch>/                     the batch
.../<batch>/<plate>                       ome.plate: rows, columns, wells with images, field_count, acquisition if
                                          stated; plate type; wells and sites in the bucket but not load_data.csv;
                                          LZW-shortened chunks
.../<plate>/<row>/<column>                ome.well: its fields; JCP2022 id, pert_type, control_type, control_name,
                                          InChIKey or gene, broad_sample
.../<plate>/<row>/<column>/<site>         image: level 0 (c, y, x) referenced; level 1 written; the source URLs
.../<batch>/illum/<plate>                 image: (c, y, x) float32, the plate's illumination functions, referenced
```

### Axes, units, levels

- **Fields** are `(c, y, x)`. Each level-0 chunk is one TIFF strip: one chunk
  per TIFF on every source but source_8, whose chunks are `(1, 4, 1024)`.
- **Channels** follow protocol order: DNA, RNA, ER, AGP, Mito, then
  `Brightfield_L`, `Brightfield`, `Brightfield_H` where present. The order
  comes from a fixed list, never from sorting the names or from file channel
  numbers, which differ between sources. An `omero` block labels and colors
  them.
- **Pixel size** (decision 5) and **position** (decision 6) come from
  `load_data.csv` or JUMP's tables. The TIFF's own resolution tags are kept
  under `tiff` in the field's attributes and never used.
- **Units** are `micrometer`.
- **Levels.** Level 0 is full resolution. Level 1 is a 16× block mean,
  zstd-compressed, so a plate viewer can draw every field.

### Referenced, written, beside

| product | how |
|---|---|
| level 0 of every field | referenced: one chunk per TIFF strip, parsed with `minus80.parsers.tiff.TiffParser` |
| level 1 of every field | written, from one decode of every TIFF |
| illumination functions | referenced: one chunk per `.npy`, from byte 128, with `minus80.core.builders.flat_binary` |
| plate, well and field documents; each well's perturbation; source URLs | written: JSON attributes |
| plate, well and field tables | Iceberg tables beside the stores |

### The tables

One Iceberg namespace, `cpg0016_jump`, in `earthmover-biology`'s catalog:

| table | grain and key | built from | rows |
|---|---|---|---|
| `plates` | plate: (`source`, `batch`, `plate`) | `plate.csv.gz`, `microscope_config.csv`, `load_data.csv` | 2,298 |
| `wells` | well: plate key + `well` | `well.csv.gz` and the perturbation tables | about 1 million |
| `fields` | field: well key + `site` | `load_data.csv` and the stores | about 7.7 million |

- **`plates`**: plate type, microscope, pixel size and its source, sites per
  well, wells and fields in the store, the plate's repo, snapshot and store
  path, and whether `plate.csv.gz` lists it.
- **`wells`**: JCP2022 id, `pert_type`, `control_type`, `control_name`,
  InChIKey, gene symbol, `broad_sample`, a boolean `is_control`, and the well's
  store path.
- **`fields`**: the field's store path, its position in micrometers and its
  acquisition time where stated, and one `url_<channel>` column per channel,
  empty where a source lacks the channel.

The field row count is the sum of `load_data.csv` rows. Keys are strings and
integers, never floats.

### Layouts considered and rejected

- **One 5-D array per plate, `(well, site, c, y, x)`.** It breaks the §2.1 MUST,
  since well and site would both be custom axes beside the channel.
- **Sites stacked per well, `(site, c, y, x)`.** It breaks the same rule.
- **Fields stitched into one image per well.** Positions are stage coordinates
  with gaps between fields, and seven sources state no position at all.
- **JUMP-lite's Zarr copies** in `cpg0016-jump/source_all/images/jump_lite_v1.0/`.
  They hold 655,101 sites from six sources, each `(5, 1080, 1280)` in one chunk,
  so they cover about a twelfth of the dataset, with the five stains and no
  brightfield. A virtual store of the TIFFs covers every source at full
  resolution.

## What has been tested

All runs read from a laptop in California, outside AWS.

- **Survey**: `workspace/load_data_csv/` listed for every source on
  2026-11-10 (counts above). Every plate folder holds `load_data.csv` and
  `load_data_with_illum.csv` only. 2,523 plates are in both the bucket and
  `plate.csv.gz`. Source_15 has 21 plates in the bucket that the table omits
  and 2 in the table that the bucket lacks; every other source matches
  exactly.
- **`TiffParser`**, one TIFF from each of sources 1, 2, 6, 7, 8 and 11, covering
  every microscope and strip layout: sources 1, 2, 7, 8 and 11 parse remotely
  in 0.23 to 0.48 s each and decode byte-identical to `tifffile.imread` through
  an Icechunk round trip. Source_8 gives 256 chunks of `(4, 1024)`. Source_6
  raises `NotImplementedError: page 0: 970 rows in strips of 6 leaves a short
  final strip`.
- **Pixel size from the parser**: 6.45 µm on Opera and Operetta (the camera's
  pixel pitch), 903,111 µm on CV7000 and CV8000 (a 72000/2560000-per-inch
  tag), none on source_8. None is the sample pixel, hence decision 5.
- **LZW over-run**, on the two known pilot strips: `TiffParser` indexes both,
  and reading through the `imagecodecs_lzw` Zarr codec fails, one with
  `ValueError: When changing to a larger dtype ...` (the decode runs 3 bytes
  long) and one with `IMCD_LZW_CORRUPT`. Shortening each reference by one
  byte decodes to tifffile's bytes. `imagecodecs.lzw_decode(strip,
  out=2332800)`, a decode bounded by the chunk's size, also returns the right
  length for both, so the upstream fix is to pass that bound in the codec.
- **Illumination**: one source_4 `.npy` header reads `<f4`, C order,
  `(1080, 1080)`, data from byte 128, and 128 + 1080 × 1080 × 4 equals the
  object's size.
- **The earlier ingest**, which built and verified one plate each of sources 1,
  2, 3, 4, 8, 10 and 11 with different code, found LZW-shortened strips on
  every Opera and Operetta plate it built: 19 on source_1's UL000109, 11 on
  source_3's BR5867a3, 8 on source_4's BR00117035, 7 on source_11's EC000001.

Not yet run: any part of this ingest, a commit of a full plate, the TIFFs of
sources 3, 4, 5, 9, 10, 13 and 15, and the illumination headers of any source
but source_4. Sources 7 and 13's `.npy` files are 1080 × 1280 × 2 + 128 bytes,
which suggests uint16 rather than float32; their headers decide.

## Judgement calls

1. **Where one repo ends.**
   - *Evidence:* a reader fetches the whole snapshot on open. A 9-site,
     384-well plate is about 10,800 nodes (3,456 fields, each a group and two
     arrays, plus wells and rows) and added about 0.63 MB to the earlier
     store's snapshot. Icechunk's snapshot cache holds 500,000 nodes by
     default, about 45 such plates.
   - *Options:*
     - One repo for the dataset: about 25 million nodes and a snapshot of
       about 1.4 GB. No reader can open it today.
     - One repo per source (12): 55 to 303 plates, a snapshot of about 60 to
       190 MB, and up to seven times the default cache.
     - **One repo per batch (136)**: 1 to 70 plates, a median of 13; the
       largest is source_15's `2021_12_03_Batch1`. A snapshot of about 8 MB at
       the median and 45 MB at the largest.
   - *Recommended:* one repo per batch, named
     `cpg0016-jump-<source>-<batch>`. It is the only option every reader can
     open at default settings. The `plates` table lists every plate's repo, so
     a query across the dataset never needs a listing of repos.
   - *Cost:* 136 repos in the organization, and a reader who wants two batches
     opens two repos.
   - *Reversible:* every repo keeps the gallery's paths, so once Icechunk
     serves snapshots of this size, `minus80.core.repo_copy` merges the
     batches into fewer repos without renaming a node.
2. **Which plates.**
   - *Evidence:* 2,544 plates have a `load_data.csv`; 2,525 are in
     `plate.csv.gz`. The 21 bucket-only plates are all on source_15.
   - *Recommended:* ingest every plate with a `load_data.csv`, source_6
     excepted, which is 2,298. A plate missing from `plate.csv.gz` has no plate
     type or perturbations; its wells say so, and the `plates` table records
     that the table omits it. Record the 21 plates in LAYOUTS.md and ask
     JUMP's maintainers why they are absent.
3. **Source_6.**
   - *Evidence:* its 970-row images are written in strips of 6 rows, so the
     last strip holds 4. Zarr's regular chunk grid cannot reference it, and
     zarr-python opens a rectilinear grid only when the reader opts in.
     Padding or merging strips breaks the rule that a chunk is the file's own
     storage unit.
   - *Recommended:* leave its 246 plates out and record why, as the earlier
     ingest did (2026-10-09). Revisit when rectilinear grids open by default.
4. **Source_8's strips.**
   - *Evidence:* each TIFF is 256 strips of 4 rows, so its 216 plates hold
     about 955 million chunk references, more than the rest of the dataset
     together (about 46 million). At zarr's default `async.concurrency` of 10 a
     full-resolution field read in about 8 s from a laptop, and at 128 in 0.7
     to 0.9 s.
   - *Recommended:* keep one chunk per strip, the file's own storage unit, and
     state in each source_8 plate's attributes that readers should raise
     `async.concurrency`. Ingest source_8 last, after the commit cost of a
     source_8 batch is measured.
5. **Pixel size.**
   - *Evidence:* the TIFF tags state the camera pitch or a placeholder (see
     "What has been tested"). Sources 1, 4, 9, 11 and 15 state
     `Metadata_ImageResolutionX/Y` in `load_data.csv`; JUMP's
     `microscope_config.csv` gives a value per source. The CV values in
     `microscope_config.csv` have not been checked against the instrument
     files.
   - *Recommended:* take `load_data.csv`'s value where stated, else
     `microscope_config.csv`'s, and record which in the source group.
6. **Positions and times.**
   - *Evidence:* `load_data.csv` states stage positions and acquisition times
     only on sources 1, 4, 9, 11 and 15. Elsewhere they are in instrument files
     (`MeasurementData.mlf` on CV sources, `Index.idx.xml` on source_3, each
     TIFF's ImageDescription on source_8) or nowhere (source_7). Source_10's
     `AbsTime` is the constant `531`.
   - *Recommended:* place fields in their wells where `load_data.csv` states a
     position (Y negated, as checked on the pilot's Opera Phenix plates), and
     declare position and time not recorded elsewhere. Reading the instrument
     files is a later task. Check the sign of Y on one Operetta plate and on
     source_9 before trusting it there.
7. **Channels.**
   - *Evidence:* stains and brightfield planes differ by source (the table
     above); source_15 has no RNA images, though its illumination folder holds
     an `IllumRNA.npy`.
   - *Recommended:* each plate's `c` axis holds the channels its
     `load_data.csv` lists, in protocol order, with no empty planes for absent
     channels. The illumination image follows the same channels, so source_15's
     `IllumRNA.npy` is left out and listed.
8. **Field names.**
   - *Evidence:* sources 7 and 13 number sites 0 to 8 in `load_data.csv` and
     1 to 9 in their file names; every other source counts from 1.
   - *Recommended:* name each field by `Metadata_Site` as `load_data.csv` gives
     it, so a path and a field-table row always carry the source's own number.
     LAYOUTS.md records the offset on sources 7 and 13.
9. **What `load_data.csv` omits.**
   - *Evidence:* source_9's image folders hold rows `AA` to `AF` of their
     1536-well plates, 5,760 TIFFs per plate that `load_data.csv` does not
     list. Source_6's H14 on one plate has 8 sites.
   - *Recommended:* follow `load_data.csv`, as the pilot plan decided. Each
     plate lists the wells and sites its image folder holds beyond
     `load_data.csv`, with their URLs, as present but not listed.
10. **Perturbations and controls.**
    - *Evidence:* `well.csv.gz` gives each well a JCP2022 id;
      `perturbation_control.csv` marks `negcon`, `poscon` and `empty`.
    - *Recommended:* join by exact key at a pinned commit of
      jump-cellpainting/datasets. `pert_type` is `trt`, `control` or `empty`,
      and `control_type` is `negcon` or `poscon`. A well with no row stays
      blank, and each plate records its join coverage.
11. **Layers left for later.** Well-level profiles (one Parquet per plate,
    which Iceberg can register in place), per-cell features (SQLite, 16 to 62
    GB per plate), JUMP's deep-learning embeddings and the instrument metadata
    are each a plan of their own. The pilot's experience is that profiles are
    the layer most scientists use, so they come next.

## Steps, scope and risk

**Phase 0, code.** Write `ingests/cellpainting-gallery/`: `ingest.py` (one
plate from its `load_data.csv`), `jump.py` (JUMP's tables at a pinned commit),
`verify.py`, `LAYOUTS.md` (ported from the earlier ingest's per-source notes)
and a notebook that opens a store cold. Add the LZW length check to
`minus80.parsers.tiff`, with a test on a real shortened strip, and record the
bounded-decode fix in `docs/upstream.md`.

**Phase 1, laptop trial.** Two wells of one plate per source, into a local
repo: parse, write, verify against tifffile, and open in a plate viewer. Read
every source's illumination header.

**Phase 2, one plate per source. Needs approval to create one repo per trial
batch in `earthmover-biology` and to run Coiled.** One full plate from each
of the 12 sources on Coiled VMs in us-east-1, each on a `trial-<source>`
branch: index, decode once for the overview and the LZW check, commit. Time
each commit, and check Y's sign on the Operetta and source_9 plates. Run
`verify.py`, open each plate, and merge to `main`.

**Phase 3, the dataset. Needs approval for the run.** Batch by batch, one VM
per plate, one session per plate forked and merged once per batch. Source_8
goes last. Progress prints at a steady interval and is forwarded to the
launching terminal.

**Phase 4, the tables. Each write is approved separately.** Build `plates`,
`wells` and `fields`, check each row count against its source, and pin each
row to the repo and snapshot it describes.

**`verify.py`**, in about a minute per batch:

- metadata gates on every field: schema, axes, pixel size, provenance, and
  completeness against `load_data.csv`;
- references against `TiffParser`, and decoded pixels against tifffile, on
  one field per plate and every shortened chunk of one plate.

**In scope:** the images and illumination functions of 2,298 plates, and the
three tables.

**Not a goal:** source_6; profiles, per-cell features, embeddings and
instrument metadata (decision 11); the pilot dataset `cpg0000-jump-pilot`,
whose nine plates are already in `earthmover-biology/cellpainting-gallery`.

**Writes that need approval:**

- creating the repos and the Iceberg namespace in `earthmover-biology`;
- the Phase 2 trial and the Phase 3 run;
- each table write.

**Cost.** The earlier ingest indexed a 9-site plate in about 190 s on one
`m6i.2xlarge` and built its overview in about 7 minutes. At that rate the
dataset is about 400 VM-hours, about $150 on demand. Reads stay in us-east-1.

**Risks.**

- **Commit cost.** About 6,900 arrays per 9-site plate and 90,000 for a
  median batch. OpenCell committed 18,500 arrays in 115 s; a batch commit is
  unmeasured until Phase 2.
- **Source_8's manifests.** 955 million references, about 4.4 million per
  plate and 246 million in its largest batch, `J2`; neither the commit nor the manifest size of a source_8 batch has been
  measured.
- **Readers need imagecodecs-zarr** for level 0 on LZW sources, since
  `imagecodecs_lzw` is outside the Zarr core. The root attributes say so;
  level 1 uses only zstd.
- **An LZW strip no cut fixes.** The overview pass checks every strip, and a
  strip whose shortened forms all decode wrong keeps its full reference and
  fails to read. None has been seen; each would be listed in its plate's
  attributes.
- **Counts are estimates** until every `load_data.csv` is read in Phase 3; the
  plan quotes them from one plate per source.
