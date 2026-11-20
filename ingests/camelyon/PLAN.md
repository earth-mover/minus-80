# CAMELYON: ingest plan

## Summary

`earthmover-biology/camelyon` holds the 399 CAMELYON16 slides, copied from
`earthmover-public/camelyon` by run 1. This plan adds the 1,000 CAMELYON17
slides beside them, in the same repo, as new commits on top of that history,
so the store shows itself growing. The CAMELYON17 slides take the CAMELYON16
layout: each slide an OME-Zarr image whose chunks are the TIFF's own 512 × 512
JPEG tiles in `s3://camelyon-dataset` (us-west-2, anonymous, CC0), its mask a
label image, its outlines arrays and QuPath GeoJSON, its labels in the
attributes. CAMELYON16 is not rewritten.

The ingest that built CAMELYON16 is now in this directory, rebuilt on
`minus80`. Rebuilt into a local repo, three CAMELYON16 slides match the
published ones in 102 of their 106 `zarr.json` documents byte for byte and
in all 256,093 chunk references; the four exceptions are below.

**Asked of the reviewer:** approve the CAMELYON17 layout and run 2, and decide
the judgement calls below. Each has a recommended default, which the code and
the trial already follow.

| # | Decision | Recommended |
|---|---|---|
| 1 | Where a CAMELYON17 slide sits | `CAMELYON17/<patient>/<node>/`, the patient group holding the pN stage |
| 2 | CAMELYON17's OME-Zarr version | 0.6, as AGENTS.md asks of new output; CAMELYON16 stays 0.5 |
| 3 | Where the labels from `stages.csv` go | the container's and groups' attributes, as CAMELYON16's do |
| 4 | Names for the five centers, which no file states | write the inferred mapping as `center_names`, with its evidence |
| 5 | The root attributes once both challenges are in | rewrite title, description, citation and source for both |
| 6 | Spelling in stored text | keep CAMELYON16's published strings ("tumour", "centre") for both challenges |
| 7 | One rebuilt attribute that changes | `regenerates_source` names `minus80.parsers.asap.to_xml` instead of a repository that no longer exists |
| 8 | How CAMELYON17 is committed | ten commits of 100 slides, each with a complete table |

Run 2 is the one write that needs approval: about 20 minutes on one Coiled VM
in us-west-2, under $1 (*Steps, scope and risk*).

## The archive

The CAMELYON challenges published whole-slide images of H&E-stained sentinel
lymph node sections from breast cancer patients
([Litjens et al. 2018](https://doi.org/10.1093/gigascience/giy065)).
CAMELYON16 asked for metastasis detection on 399 slides
([Ehteshami Bejnordi et al. 2017](https://doi.org/10.1001/jama.2017.14585));
CAMELYON17 asked for patient-level pN staging from 1,000 slides, five lymph
nodes for each of 200 patients from five Dutch medical centers
([Bándi et al. 2019](https://doi.org/10.1109/TMI.2018.2867350)). Both are CC0
(`license.txt` in each prefix; the AWS Open Data registry entry says
`License: CC0`). People use them to train and benchmark tumor detectors and
slide classifiers, and as the standard example of stain and scanner variation
across centers.

`s3://camelyon-dataset/CAMELYON17/`, listed on 2026-11-19, holds 1,156
objects:

| Prefix | Objects | Size |
| --- | ---: | ---: |
| `images/` | 1,000 `patient_NNN_node_N.tif` | 2,973.8 GB |
| `masks/` | 100 `<slide>_mask.tif`: the 50 annotated slides, and 50 negative slides | 2.39 GB |
| `annotations/` | 50 ASAP XML files, 10 slides per center | 1.9 MB |
| `evaluation/` | `evaluate.py`; `example.csv`, a sample submission | 22 KB |
| root | `README.md`, `license.txt`, `checksums.md5`, `stages.csv` | 100 KB |

`stages.csv` has 600 rows: the pN stage and center of each of the 100
training patients (000 to 099), and the label of each of their 500 slides
(negative 318, itc 36, micro 59, macro 87). The bucket labels none of the 100
test patients and states no center for them. `checksums.md5` lists every file
but itself and the 50 negative slides' masks. LAYOUTS.md has the joins and
every count.

**The five centers' TIFFs.** A header census of all 1,100 CAMELYON17 TIFFs
(no pixels decoded; two slides per center read in full) found two dialects:

| Center | Dialect | Compression | Tiles | Levels | Pixel size, and where it is stated | Extra pages |
| --- | --- | --- | --- | --- | --- | --- |
| 0, 1, 3 | generic BigTIFF written by ASAP's converter | RGB JPEG, shared `JPEGTables` | 512² | 9 | resolution tags, 0.243094 µm (a 3D Histech scan) | none |
| 2 | same | same | 512² | 8 to 10 | resolution tags, 0.2263 to 0.2269 µm (a Hamamatsu scan) | none |
| 4 | Philips-format BigTIFF, `Software` "Philips DP v1.0" | YCbCr JPEG, shared `JPEGTables` | 512² | 9 or 10 | ImageDescription XML only, 0.25 µm, scanned and representation alike | Macro |
| test | 400 generic, 100 Philips | as above | 512² | 8 to 10 | as above | Macro on the Philips ones |

**One code path handles them all.** These are the two dialects CAMELYON16
already has (`normal_144` is generic; 398 slides are Philips), so the ported
code reads every CAMELYON17 slide unchanged but for one fix, below. Two
findings needed code:

- **Cropped Philips levels.** In 44 of the 200 Philips slides some reduced
  levels stop up to a tile short of level 0 halved (21 in width, 27 in
  length). Comparing a cropped level with the level above it averaged down
  shows a shift of 0 pixels in every tested region, where rescaling would
  shift by 32 to 60, so the scanner cropped the level and kept its 2× pixel
  size. The ingest gives a Philips level 2^k times level 0's pixel size, which
  for every CAMELYON16 slide equals the shape ratio it used before.
- **A tifffile bug.** For a level cropped in length, tifffile 2026.10.15
  reads the length as level 0 halved, past the page's last row of tiles.
  `minus80.parsers.tiff.pages.level_array` now caps a level at its page's own
  tags ([docs/upstream.md](../../docs/upstream.md)); without the fix, 4 of 15
  Philips slides in one trial were refused.

## Proposed layout

The CAMELYON16 layout, extended (LAYOUTS.md has every node, attribute and
codec; the figures are [source](diagrams/source.svg),
[mapping](diagrams/mapping.svg) and [store](diagrams/store.svg)):

```
/                               root: title, license, citations, codecs a reader needs
├── CAMELYON16/                 unchanged, OME-Zarr 0.5
└── CAMELYON17/                 bioformats2raw.layout container, OME-Zarr 0.6
    ├── OME/                    series: every image, in slide order
    ├── documents/              checksums.md5 (71 KB), native
    └── patient_004/            pN stage, center, split, its five slides
        └── node_4/             slide patient_004_node_4, its row of the table
            ├── image/          (c, y, x), 7 to 10 levels, chunks (3, 512, 512)        virtual
            │   └── labels/mask/   the mask, levels the file lacks derived              virtual + native
            ├── macro/          the Philips Macro page, one chunk                      virtual
            └── shapes/         the outlines as arrays and GeoJSON                      native
```

- **Referenced:** every slide, Macro and mask tile, 1,100 files.
- **Written natively:** the outlines, the coarse mask levels a mask file
  lacks (11 of 100), `checksums.md5`, and the other text files in attributes.
- **Iceberg:** nothing (decision 3).

Expected size: the trial wrote 1.6 MB of repo per slide, so CAMELYON17 adds
about 1.6 GB and about 13,000 nodes beside CAMELYON16's.

## What has been tested

All runs were from a laptop, reading the bucket in us-west-2, into local
Icechunk repos. No Arraylake repo or bucket was written.

**Port fidelity.** `tumor_056` (3D Histech Philips), `tumor_091` (Hamamatsu
Philips) and `normal_144` (generic) were rebuilt with the ported code and
compared with `earthmover-biology/camelyon` at `MNPFXPY2MK0EX78VXRZG`:

- root `zarr.json`: identical;
- slide nodes: 102 of 106 `zarr.json` identical byte for byte;
- chunk references: all 256,093 identical; the 14 native chunks compared
  (outline arrays, documents, the derived mask level of `tumor_056`) identical
  except the two GeoJSON arrays;
- the four differing nodes: `shapes` on `tumor_056` and `tumor_091`, whose
  `regenerates_source` changes by decision 7; and their `shapes/geojson`, whose
  polygons are equal as geometries (shapely `equals`, zero symmetric
  difference) but list vertices differently. Run on this laptop, the original
  code writes the same bytes as the port, so the GEOS build of the platform
  differs, not the code; run 2 runs on Linux, as the published store did.
- `CAMELYON16` and `OME` differ only because the local container holds 3
  slides.

**CAMELYON17 trial.** Two slides per center, ten in all: one annotated and
one negative-with-mask slide from each, including `patient_081_node_0`, a
Philips slide with cropped levels and a derived mask level.

| Measure | Result |
| --- | --- |
| Per slide, one thread | 4.2 to 6.4 s, median 5.1 s (image and mask headers, outlines) |
| 10 slides, 10 threads | 7 s parsing, 11 s in all |
| 65 slides, 32 threads | 17 s parsing, 3 s commit; peak memory 4.3 GB |
| 200 slides, 32 threads, two batches of 100 | 60 s in all, 5 to 6 s per commit; peak memory 7.8 GB |
| `verify.py` on the 10 | passes in 27 s |

`verify.py` checked: every slide's mask and outlines present exactly where
the bucket has them, and its labels equal to `stages.csv`; the six source
files verbatim; five outline tables rebuilding their XML byte for byte, with
the MD5s `checksums.md5` lists, and their GeoJSON agreeing with the mask's
tumor pixels away from the edges (0 interior pixels off on all five); on one
generic and two Philips slides, a level-0 tile, the whole coarsest level and
a mask tile identical to tifffile's decode, and the derived mask level within
the file's values; all eight OME-Zarr gates.

Threads scale: one thread parsed 10 slides in 51 s, ten threads in 7 s. The
work waits on range reads, so threads, not processes. A fork holds about
50 MB per slide until it is committed, so slides are committed 100 at a time.

`test_ingest.py` runs the whole ingest on `fixture.py`'s small copy of the
bucket (both dialects, a cropped Philips level, masks with a missing level,
a tissue file that is a copy of its mask, outlines with a hole) in 3 s, with
no network: 15 tests.

## Judgement calls

**1. Slides under their patient.** CAMELYON17's labels are per patient (pN
stage) as well as per slide, so `CAMELYON17/patient_004/node_4` puts the
patient's row on a group that holds its five nodes. Rejected: flat
`CAMELYON17/patient_004_node_4/`, like CAMELYON16, which keeps every slide at
one depth and the challenge's name as the path, but leaves the pN stage
nowhere but a table. The slide table keeps the flat name in `slide`, and the
QuPath extension finds images at any depth. *Recommended: nested.*

**2. OME-Zarr 0.6 for CAMELYON17.** AGENTS.md asks for 0.6 output and keeps
published 0.5 stores at 0.5, so the store mixes versions by container. The
QuPath extension reads both. Rejected: 0.5 for consistency within the repo,
against the rule. *Recommended: 0.6.*

**3. Labels in attributes.** AGENTS.md sends tabular data without a Zarr
convention to an Iceberg table. The slide table has a convention here:
`minus80.ome.annotation_table`, which CAMELYON16 already uses, one row per
image keyed by its path, with each row repeated on its slide group. The
CAMELYON17 table and patient table add about 250 KB to the container's
`zarr.json`, beside CAMELYON16's 187 KB. Rejected: Iceberg tables beside the
store, which would split the labels across two systems for the two
challenges and make a reader of one slide go elsewhere for its label.
*Recommended: attributes.*

**4. Center names.** `stages.csv` numbers the centers 0 to 4. The dataset
paper's per-center label counts match `stages.csv` exactly for three centers
and within three slides for two, and the scanners agree; the README lists the
centers in a different order, which would have mapped three of them wrongly.
The mapping is 0 CWZ, 1 RST, 2 UMCU, 3 RUMC, 4 LPON (LAYOUTS.md, *Centers*).
The `center` column stays verbatim. Rejected: leave the numbers unnamed,
which is safer and leaves a reader unable to tell which hospital is which.
*Recommended: write `center_names` with the evidence in
`center_names_decision`.*

**5. Root attributes.** The root now says "CAMELYON16 lymph node metastases"
and cites one paper. Once CAMELYON17 is in, the ingest rewrites `title`,
`description`, `citation` (a mapping of three papers), `source` and
`license_text` for both. A reader who parses `citation` as a string breaks.
*Recommended: rewrite.*

**6. Spelling.** Four CAMELYON16 strings say "tumour" and "centre". The port
keeps them so the rebuilt nodes match the published ones, and CAMELYON17
reuses them so the two challenges' attributes match. Rejected: American
spelling for CAMELYON17 only. *Recommended: keep.*

**7. `regenerates_source`.** The published value names "asap.to_xml in
ingest-camelyon", a repository that is not public. The port names
`minus80.parsers.asap.to_xml`. The 160 published CAMELYON16 outline groups
keep the old text unless rebuilt. *Recommended: the new text, without
rewriting CAMELYON16.*

**8. Ten commits.** One commit per 100 slides keeps a batch's memory near
8 GB, and each commit rewrites the table, so every snapshot describes exactly
what it holds. A stopped run keeps its finished batches and a rerun skips
them. Rejected: one commit, which needs about 50 GB of memory. *Recommended:
ten commits; a tag `camelyon17` on the last, if wanted.*

Recorded in LAYOUTS.md and unchanged from CAMELYON16: the scanned pixel size
for Philips files, tissue maps as images, documents over 64 KB as byte
arrays, outlines as arrays plus GeoJSON, derived mask levels, the open pixel
convention of the outlines.

## Steps, scope and risk

1. **Approve this plan** and decisions 1 to 8.
2. **Run 2** (`runs/02_camelyon17.sh`): one c6in.4xlarge (16 vCPUs, 32 GB) in
   us-west-2 runs `ingest.py --challenge CAMELYON17` into
   `earthmover-biology/camelyon` with 32 threads, then `verify.py`. This is
   the write that needs approval. Estimate: parsing 4 to 8 minutes (the
   laptop rate of 3.3 slides a second, likely faster beside the bucket), ten
   Arraylake commits of 30 to 60 s each (run 1 committed 50 CAMELYON16 slides,
   with more nodes each, in about 25 s), install and verify about 5 minutes:
   15 to 25 minutes. Cost: under $1 of VM time ($0.91 an hour on demand), no
   egress (same region), 1.6 GB of repo storage.
3. **Write REPORT.md**, with run 2's numbers recorded in the run script.

**In scope:** CAMELYON17 images, masks, outlines, labels and text files.
**Out of scope:** rebuilding CAMELYON16 (published, and identical in every
reference); deleting anything; making the repo public; the Grand Challenge
datasets beyond CAMELYON, all non-commercial.

**Risks.**

- **Untested against Arraylake.** The trial wrote only local repos. Forks are
  shared between threads, not pickled, so the Arraylake session's credentials
  never cross a process; merge-then-commit has not run on Arraylake before.
- **Commit time.** Each commit adds 100 slides to a snapshot that grows to
  about 27,000 nodes; Arraylake commits were not measured beyond run 1's.
  A slow commit shows in the heartbeat line every 30 s.
- **Memory.** 7.8 GB peak per batch on the laptop; the VM has 32 GB.
- **GeoJSON bytes.** The GEOS build changes vertex order, so a CAMELYON17
  GeoJSON written on Linux and one written on macOS differ in bytes, not in
  geometry.
