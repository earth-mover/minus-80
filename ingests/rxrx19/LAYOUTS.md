# RxRx19a and RxRx19b: how the zips map onto the store

Recursion published two COVID-19 Cell Painting screens under CC BY 4.0 on the
public bucket `gs://rxrx`, readable anonymously over `gs://` or
`https://storage.googleapis.com/rxrx/`:

- [RxRx19a](https://www.rxrx.ai/rxrx19a) images SARS-CoV-2 infection in human
  renal cortical epithelial cells (HRCE) and Vero cells, treated with 1,669
  compounds ([Heiser et al. 2020](https://doi.org/10.1101/2020.04.21.054387)).
- [RxRx19b](https://www.rxrx.ai/rxrx19b) images endothelial cells (HUVEC)
  exposed to a cytokine-storm cocktail, treated with 1,856 compounds
  ([Cuccarese et al. 2020](https://doi.org/10.1101/2020.08.02.233064)).

`PLAN.md` gives the choices behind the store; this file says what the bucket
holds and where each piece lands. The figures in `diagrams/` draw both, from
the same values.

## Files

Each dataset is three zips:

| object | size | members |
|---|---|---|
| `RxRx19a/RxRx19a-images.zip` | 477.7 GB | 1,527,600 PNGs, a LICENSE and a README.md |
| `RxRx19a/RxRx19a-metadata.zip` | 4.0 MB | `metadata.csv`, 305,520 rows |
| `RxRx19a/RxRx19a-DL-embeddings.zip` | 1.5 GB | `embeddings.csv`, a 1024-value embedding per site |
| `rxrx19b/rxrx19b-images.zip` | 438.3 GB | 422,304 PNGs, a LICENSE and a README.md |
| `rxrx19b/rxrx19b-metadata.zip` | 2.0 MB | `metadata.csv`, 70,384 rows |
| `rxrx19b/rxrx19b-dl-embeddings.zip` | 42.5 MB | `embeddings.csv`, a 128-value embedding per site |

Both image zips are zip64. Every PNG is stored, not deflated; only the LICENSE
and README.md are deflated. Each PNG is one contiguous byte range, so it can be
a chunk as it is.

The bucket also holds `RxRx19a/metadata.v1.zip` and `metadata.v2.zip`, of
which `RxRx19a-metadata.zip` is the same size as v2, and a loose copy of the
images under `RxRx19a/images/`. The loose copy is incomplete: its first well
lacks two of site 1's five channels, and it has a misspelled `HCRE-1`
directory beside `HRCE-1`. The store references the zip, which matches
`metadata.csv` site for site.

## Where a PNG's bytes start

A zip member is a local header (30 fixed bytes, then the name, then an extra
field) followed by the data. The central directory gives where each local
header starts, but not how long its extra field is, and that length can differ
from the directory entry's own extra field. Here every local header's extra
field is 28 bytes (a Unix timestamp and Unix user and group ids), against 36 in
the directory entries.

The directory alone proves this for every member, with no request: members
sit back to back, so each member's span, from its local header to the next
one's, must be exactly `30 + name length + 28 + size`, and in both zips every
span is. Each PNG's data therefore starts at
`local_header_offset + 30 + name length + 28`. `diagrams.py` repeats this check
and stops if any member fails it.

## Paths

```
RxRx19a/images/HRCE-1/Plate1/AA02_s2_w3.png
               │      │      │    │  └ channel 3 of 5 (RxRx19a) or 6 (RxRx19b)
               │      │      │    └ site 2 of 4 (RxRx19a) or 1 (RxRx19b)
               │      │      └ well: row AA, column 02
               │      └ plate number within the experiment
               └ experiment: cell type and run
```

| dataset | experiment | plates | wells | sites | PNGs |
|---|---|---|---|---|---|
| RxRx19a | HRCE-1 | 26 | 34,840 | 139,360 | 696,800 |
| RxRx19a | HRCE-2 | 27 | 36,180 | 144,720 | 723,600 |
| RxRx19a | VERO-1 | 2 | 2,680 | 10,720 | 53,600 |
| RxRx19a | VERO-2 | 2 | 2,680 | 10,720 | 53,600 |
| RxRx19b | HUVEC-1 | 53 | 70,384 | 70,384 | 422,304 |

Every RxRx19a plate holds 1,340 wells and every RxRx19b plate 1,328, all with
every site and every channel present.

## Wells: the letters are rows

Well names are a letter and a two-digit number. The letters run B to AE (30
values) and the numbers 02 to 47 (46 values): the inner rows and columns of a
1536-well plate, which has 32 rows (A to AF) and 48 columns (1 to 48), with
the edge wells unused. So `AA02` is row AA, column 2.

Both datasets' READMEs say the opposite: "Well location on plate (column AA,
row 2)". The store follows the data, and keeps the source's well name as
`well_id` in each well's annotations.

## Hierarchy

```
<dataset>/<experiment>/Plate<n>/<row>/<column>/<site>/0
rxrx19a  /HRCE-1      /Plate1  /AA   /2       /2     /0
```

| source | store |
|---|---|
| dataset zip | top-level group `rxrx19a` or `rxrx19b` |
| `<experiment>/` | group, carrying the experiment's cell type |
| `Plate<n>/` | OME-Zarr HCS plate, declaring all 32 rows and 48 columns |
| well `AA02` | row group `AA`, well group `AA/2` |
| site `s2` | field image `AA/2/2`, named by the source's site number |
| channel `w3` | plane 2 of the field's `c` axis |
| one PNG | one chunk `(1, y, x)` of array `0`, decoded by `imagecodecs_png` |

## metadata.csv

One row per site, with columns `site_id`, `well_id`, `cell_type`,
`experiment`, `plate`, `well`, `site`, `disease_condition`, `treatment`,
`treatment_conc` (µM) and `SMILES`. Every row matches a site with PNGs, and
every site with PNGs has a row. All but `site_id` and `site` are the same for
every site of a well, and those go into the well's annotations; the whole table
goes to an Iceberg table, outside the Zarr hierarchy, keyed by `site_id`.

Some fields are blank in the source, and the store keeps them as `null`:

| dataset | blank `treatment` | blank `disease_condition` |
|---|---|---|
| RxRx19a | 42,336 sites: all Mock and UV Inactivated sites, 17,192 Active SARS-CoV-2 sites, and the 6,904 with no condition | 6,904 sites |
| RxRx19b | 5,800 sites | 264 sites |

The source does not say what a blank condition means. The store does not
guess.

`SMILES` holds ChemAxon extended SMILES (CXSMILES): 1,554 of RxRx19a's
1,670 distinct values end in a `|c:…|` extension, which plain SMILES parsers
reject. The store names the well annotation `cxsmiles`, value unchanged.

## embeddings.csv

One row per site: `site_id`, then `feature_0` … `feature_1023` in RxRx19a and
`feature_0` … `feature_127` in RxRx19b. Its rows are not in `metadata.csv`'s
order (the first is `HRCE-1_10_AA02_1`), so the store places each row by its
`site_id`. The values are the shortest decimal form of float32 numbers: every
value of the rows checked prints back identically from float32. The store
holds them as a native Zarr array `embeddings` of shape (site, feature),
float32, beside a `site_id` array that gives each row's site in
`metadata.csv`'s order.

## Pixel size and stains

Neither the PNGs, `metadata.csv`, nor the READMEs copied into the 2020
metadata zips state either. The READMEs in
[recursionpharma/rxrx-datasets](https://github.com/recursionpharma/rxrx-datasets)
do, as of commit `19883e707d` (October 2024):

- **Pixel size.** Both `rxrx19a/README.md` and `rxrx19b/README.md` say
  "Physical resolution: 0.65 micron/pixel". RxRx19b's 2048-pixel PNGs are the
  instrument's images, so the store gives them 0.65 µm. RxRx19a's 1024-pixel
  PNGs are, per its dataset page, "downsampled from the original 2048x2048
  16-bit versions", so the store gives them 1.30 µm. Its README does not say
  whether its 0.65 describes the originals or the PNGs, and RxRx1's README
  gives the same 0.65 for 512-pixel images, which suggests one constant copied
  between READMEs. This is a judgment call, recorded in `PLAN.md`.
- **Stains.** `rxrx19a/README.md` maps the channels: `w1` Hoechst 33342
  (nucleus), `w2` Concanavalin A (membrane glycoproteins), `w3` Phalloidin
  (actin), `w4` Syto14 (RNA), `w5` Wheat germ agglutinin (Golgi). The store
  labels RxRx19a's channels `DNA`, `ER`, `Actin`, `RNA` and `Golgi`, the Cell
  Painting protocol's short names for what each stain marks, as the JUMP Cell
  Painting data labels them, so that an app can pick a channel by a fixed key.
  The README calls Concanavalin A's target "membrane glycoproteins"; Cell
  Painting names that channel ER, and the store follows Cell Painting. The
  README's stain names are in the dataset group's `channels`. `rxrx19b/README.md` maps none; its
  dataset page lists the same five stains plus MitoTracker, but nothing says
  which file is which, so the store labels RxRx19b's channels `w1` … `w6`.

## Display windows

The PNGs are 8-bit, and every stain uses a small part of the range: over a
plate's Mock wells, the 99.9th percentile of the Hoechst channel is between 25
and 90 of 255. A viewer that opened each channel at 0–255 would show the
nuclei nearly black. Each site's omero window is its plate's, per channel:
from the 0.1th to the 99.9th percentile of the pixels of 8 sites of the
plate's Mock wells (uninfected, untreated), the baseline every other well is
compared with. Every RxRx19a plate has 40 Mock wells. These 2,280 PNGs are
the only pixels the ingest decodes. Computing the windows takes about a minute
from a laptop, and the result is cached beside the central directories.
