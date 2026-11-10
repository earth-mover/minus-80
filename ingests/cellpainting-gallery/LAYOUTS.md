# Cell Painting Gallery, JUMP: how the bucket maps onto the store

The JUMP Cell Painting dataset ([Chandrasekaran et al.
2023](https://doi.org/10.1101/2023.03.23.534023)) is `cpg0016-jump` in the
Cell Painting Gallery, the public bucket `s3://cellpainting-gallery`
(us-east-1, anonymous, CC0 1.0). Thirteen sites ("sources") imaged U2OS cells
treated with compounds, ORF overexpression and CRISPR knockouts, on five kinds
of microscope. `PLAN.md` gives the choices behind the store; this file says
what the bucket holds and where each piece lands.

Anonymous reads need `MINUS80_S3_ANONYMOUS=1`.

## Files

Under `cpg0016-jump/<source>/`, for a plate `<plate>` of batch `<batch>`:

| path | what | in the store |
|---|---|---|
| `workspace/load_data_csv/<batch>/<plate>/load_data.csv` | one row per (well, site), one `URL_Orig<channel>` column per channel | defines the plate; not stored |
| `images/<batch>/images/<plate folder>/…` | one TIFF per site and channel | level 0 of each field, referenced |
| `images/<batch>/illum/<plate>/<plate>_Illum<channel>.npy` | CellProfiler's illumination function per channel | `<batch>/illum/<plate>`, referenced |
| `workspace/profiles/<batch>/<plate>/<plate>.parquet` | well-level profiles | not yet (PLAN decision 11) |
| `workspace/backend/<batch>/<plate>/<plate>.sqlite` | per-cell features | not yet |

Each `workspace/load_data_csv/<batch>/<plate>/` folder holds `load_data.csv`
and `load_data_with_illum.csv`, nothing else. A listing of those folders on
2026-11-10 found 2,544 plates in 149 batches.

**Never build a path, and never list an image folder to define a plate.** The
plate folder's name differs by source (`<plate>__<time>-Measurement<n>` on
Opera sources, with a measurement number that varies between plates on
source_11 and a space in `Measurement 1` on source_3; `P01_<plate>` on
source_5; the plate alone elsewhere). Image folders also hold files that are
not images of the plate:

- source_8: a thumbnail beside every image, as many as the images;
- CV7000 and CV8000 sources: calibration TIFFs (`OTF_*`, `back_*`, `DC_*`,
  `SC_*`);
- source_13: five `Illum<channel>.tif` and `.npy` files;
- source_9: rows `AA` to `AF` of every 1536-well plate, which `load_data.csv`
  does not list.

Every URL comes from `load_data.csv`.

## Sources

One plate per source was probed on 2026-10-08 (the first plate of the first
batch); plate counts are from the 2026-11-10 listing.

| source | microscope | batches | plates | wells | sites | TIFF | strips | channels |
|---|---|---|---|---|---|---|---|---|
| 1 | Opera Phenix | 6 | 55 | 1,472 of 1536 (columns 2–47) | 4 | LZW, 1080 × 1080 | 1 | 5 stains + `Brightfield` |
| 2 | CV8000 | 13 | 229 | 384 | 6 | raw, 996 × 996 | 1 | 5 + 3 brightfield |
| 3 | Opera Phenix | 13 | 303 | 384 | 9 | LZW, 1080 × 1080 | 1 | 5 |
| 4 | Opera Phenix | 13 | 277 | 384 | 9 | LZW, 1080 × 1080 | 1 | 5 + 3 |
| 5 | CV8000 | 33 | 260 | 384 | 9 | raw, 998 × 998 | 1 | 5 + 3 |
| 6 | CV8000 | 13 | 246 | 384 | 9 | raw, 970 × 970 | 162 of 6 rows | 5 + 3 |
| 7 | CV7000 | 8 | 128 | 384 | 9 | raw, 1080 × 1280 | 1 | 5 |
| 8 | ImageXpress Micro Confocal | 4 | 216 | 384 | 9 | raw, 1024 × 1024 | 256 of 4 rows | 5 |
| 9 | Opera Phenix | 11 | 108 | 1,248 of 1536 (rows A–Z) | 4 | LZW, 1080 × 1080 | 1 | 5 |
| 10 | CV8000 | 18 | 222 | 384 | 6 | raw, 1000 × 1000 | 1 | 5 + 3 |
| 11 | Operetta | 5 | 180 | 384 | 9 | LZW, 1080 × 1080 | 1 | 5 + `Brightfield` |
| 13 | CV7000 | 6 | 154 | 384 | 9 | raw, 1080 × 1280 | 1 | 5 |
| 15 | Opera Phenix | 6 | 166 | 384 | 9 | LZW, 1080 × 1080 | 1 | 4 (no RNA) + 3 |

Every TIFF is uint16, single-page, one sample per pixel and no predictor. The
five stains are DNA, RNA, ER, AGP and Mito. The three brightfield planes are
`Brightfield_L`, `Brightfield` and `Brightfield_H`.

## Channels

`load_data.csv` names each channel in its `URL_Orig<channel>` column. The
file's channel number does not identify the channel, and differs by source:

| source | file channel numbers |
|---|---|
| 4 | `ch1` Mito, `ch2` AGP, `ch3` RNA, `ch4` ER, `ch5` DNA, `ch6` `Brightfield_H`, `ch7` `Brightfield_L`, `ch8` `Brightfield` |
| 11 | `ch1` DNA, `ch2` ER, `ch3` AGP, `ch4` RNA, `ch5` Mito, `ch6` `Brightfield` |
| 15 | `ch1` DNA, `ch2` ER, `ch3` AGP, `ch4` Mito, `ch5` `Brightfield`, `ch6` `Brightfield_H`, `ch7` `Brightfield_L` |
| 2 | `C01` DNA, `C02` ER, `C03` RNA, `C04` AGP, `C05` Mito, `C06` brightfield at `Z01` (`_L`), `Z02`, `Z03` (`_H`) |
| 6 | the action number (`A01`, `A02`, …) repeats across channels |

The store's `c` axis takes the channels a plate's `load_data.csv` lists, in a
fixed order: DNA, RNA, ER, AGP, Mito, `Brightfield_L`, `Brightfield`,
`Brightfield_H`. Absent channels are left out, never padded (PLAN decision 7).

Source_15's images have no RNA channel, but its illumination folder holds
`<plate>_IllumRNA.npy`. The illumination image follows the plate's channels,
so that file is not referenced, and each source_15 plate lists it.

## Pixel size

No TIFF states the sample's pixel size. `TiffParser` reports what the tags
say:

- Opera Phenix and Operetta: 6.45 µm (XResolution 4294967295/2770410 per
  centimeter), the camera's pixel pitch.
- CV7000 and CV8000: 903,111 µm (72000/2560000 per inch), a placeholder;
  source_6 states 96 per inch.
- ImageXpress (source_8): no resolution tag. Its ImageDescription states
  `spatial-calibration-x/y` = 0.7032 µm.

The store takes `Metadata_ImageResolutionX/Y` (in meters) from `load_data.csv`
where it is stated, on sources 1, 4, 9, 11 and 15, all 0.597976 µm. Elsewhere
it takes `Metadata_Pixel_Size_Microns` from jump-cellpainting/datasets'
`microscope_config.csv`. The source group records which. The TIFF tags stay
under `tiff` in each field's attributes. The CV values in
`microscope_config.csv` have not been checked against the instruments'
`MeasurementData.mlf`.

## Positions and times

`load_data.csv` states `Metadata_PositionX/Y` (meters) and `Metadata_AbsTime`
on sources 1, 4, 9, 11 and 15. A field's `translation` places it in its well
from the stage position, with Y negated. The earlier ingest checked that sign
by matching the edges of neighboring fields on Opera Phenix plates (the pilot's
BR00117016 and source_1's UL000109) and on source_11's Operetta plate
EC000001, whose fields overlap their neighbors by about 21 pixels.

Elsewhere `load_data.csv` has no position or time, and the fields declare them
not recorded:

- sources 2, 5, 6, 10 and 13 carry them in `MeasurementData.mlf` in the image
  folder;
- source_3 carries them in `Index.idx.xml`;
- source_8 carries them in each TIFF's ImageDescription (`stage-position-x/y`,
  `acquisition-time-local`);
- source_7 has no instrument file at all.

Source_5's `Metadata_X/Y` (-818, 0, 818) are in an unstated unit and are not
read. Source_10's `Metadata_AbsTime` is the constant `531` and is not a time.

## Hierarchy

```
cpg0016-jump/<source>/<batch>/<plate>/<row>/<column>/<site>   field image, (c, y, x)
cpg0016-jump/<source>/<batch>/illum/<plate>                   illumination image, (c, y, x)
```

| `load_data.csv` | store |
|---|---|
| `Metadata_Well` (`A01`, `AA16`) | well group `A/1`, `AA/16`; parsed from the label, because `Metadata_Row` and `Metadata_Col` are not reliable (source_10 writes `Col` modulo 10, source_8 writes `Row` as a letter) |
| `Metadata_Site` | field group, named by the number as given (PLAN decision 8): sources 7 and 13 count 0 to 8, every other source 1 to 9 |
| `URL_Orig<channel>` | one plane of the field's `c` axis |
| one TIFF strip | one level-0 chunk, `(1, rows per strip, width)` |

A plate declares every row and column of its physical format: A–P and 1–24 for
384 wells, A–AF and 1–48 for 1536. Only wells with images get a group.

## Keys and joins

Perturbations come from
[jump-cellpainting/datasets](https://github.com/jump-cellpainting/datasets)
`metadata/` at a pinned commit:

- `plate.csv.gz` gives `Metadata_Source`, `Metadata_Batch`, `Metadata_Plate`
  and `Metadata_PlateType` (2,525 plates: 1,876 COMPOUND, 251 ORF, 148 CRISPR,
  141 TARGET2, 77 DMSO, 20 COMPOUND_EMPTY, 8 POSCON8, 4 TARGET1);
- `well.csv.gz` gives each well's `Metadata_JCP2022`;
- `compound.csv.gz` (InChIKey), `orf.csv.gz` (gene symbol, `broad_sample`) and
  `crispr.csv.gz` (gene symbol) resolve the JCP2022 id;
- `perturbation_control.csv` marks controls as `negcon`, `poscon` or `empty`;
- `microscope_config.csv` gives each source's pixel size.

Every join is by exact key, and a well with no match stays blank.

**Plates the table and the bucket disagree on.** On source_15, 21 plates have
a `load_data.csv` but no row in `plate.csv.gz`, and 2 have a row but no
`load_data.csv`:

- in the bucket only: `PEC00001801` and `PEC00001841`, and `PEP00004062`,
  `PEP00004065`, `PEP00004066`, `PEP00004069`, `PEP00004077`, `PEP00004089`,
  `PEP00004098`, `PEP00004100`, `PEP00004102`, `PEP00004110`, `PEP00004114`,
  `PEP00004116`, `PEP00004118`, `PEP00004124`, `PEP00004128`, `PEP00004130`,
  `PEP00004132`, `PEP00004141`, `PEP00004335` (12 in `2021_12_03_Batch1`, 8
  in `2021_12_17_Batch1`, 1 in `2022_02_28_Batch1`);
- in the table only: `PEP00004458` (`2022_02_28_Batch1`) and `PEP00004421`
  (`2022_03_23_Batch1`), both COMPOUND.

Every other source matches the table plate for plate. Why the 21 plates are
missing from the table is not known.

## Source defects

- **LZW strips that decode past the image.** On Opera Phenix and Operetta
  TIFFs, about 0.06% of strips write the end-of-information code at the
  previous code width just after the code table widens. An unbounded LZW
  decode then runs 2 or 3 bytes long or fails as corrupt; tifffile, which
  bounds the decode at the strip's size, reads them correctly. The earlier
  ingest found such strips on every Opera and Operetta plate it built: 19 on
  source_1's UL000109, 11 on source_3's BR5867a3, 8 on source_4's BR00117035
  and 7 on source_11's EC000001. The ingest shortens each such reference
  (`minus80.parsers.tiff.lzw_valid_length`) and lists the shortened chunks in
  the plate's attributes.
- **Source_6's short last strip.** 970 rows in strips of 6 leave a last strip
  of 4 rows, which a regular chunk grid cannot reference. `TiffParser` raises
  `NotImplementedError: page 0: 970 rows in strips of 6 leaves a short final
  strip`, and source_6 is not ingested (PLAN decision 3).
- **Source_6's ragged plate.** On plate 110000293081, well H14 has 8 sites;
  site 9 is missing from both `load_data.csv` and the image folder.
- **Source_8's dates.** Plate A1170383's TIFFs state an acquisition date of
  2021-03-17, before the 2021-06-24 in their barcode. The store keeps both as
  written.
- **Spelling.** Source_1's `Metadata_ChannelName` spells one dye two ways
  (`SYTO 14 green jk`, `SYTO 14green jk`); the store keeps both as written.

## Illumination functions

Each `<plate>_Illum<channel>.npy` is a NumPy 1.0 file: a 128-byte header,
then the array in C order, so each file is one chunk from byte 128. Each
plate's headers are read before its illumination image is written, and dtype
and shape come from them. The headers of one plate per source, read
2026-11-10:

| source | dtype | shape |
|---|---|---|
| 1, 3, 4, 9, 11, 15 | `<f4` | 1080 × 1080 |
| 2 | `<f4` | 996 × 996 |
| 5 | `<f4` | 998 × 998 |
| 7, 13 | `<u2` | 1080 × 1280 |
| 8 | `<f4` | 1024 × 1024 |
| 10 | `<f4` | 1000 × 1000 |

Every header's data offset is 128, and 128 plus the array's bytes is each
object's size. Sources 7 and 13 store their functions as uint16, not float32.

Each plate's `load_data_with_illum.csv` names one `URL_Illum<channel>` per
channel. On source_15's PEC00001783 it names `PEC00001783_IllumRNA.npy` for
ER, and that file and `PEC00001783_IllumER.npy` are byte-identical (SHA-256
`039363ccba89dc77…`). The store references `_IllumER.npy` for ER and lists
`_IllumRNA.npy` as not referenced.
