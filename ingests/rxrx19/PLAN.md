# RxRx19a and RxRx19b (Recursion, gs://rxrx): ingest plan

## Summary

The plan references Recursion's two COVID-19 Cell Painting screens, RxRx19a
and RxRx19b, in place in their zips on `gs://rxrx`, as OME-Zarr 0.6 HCS plates
in one Icechunk repo. Nothing is copied except the embeddings.

Both datasets are CC BY 4.0. Each is published as one zip of PNGs, 916 GB in
all, beside a small metadata zip and an embeddings zip. Every PNG in the zips
is stored, not deflated, and every member's local header has the same 28-byte
extra field, so each PNG's byte range can be computed from the zip's central
directory alone: the store's 1,949,904 references are built with no read of
image data. Today the only way to look at one well is to download a 440 GB
zip.

- **Images.** One HCS plate per source plate, 110 in all. Each site of a well
  is an OME-Zarr image with one `(channels, y, x)` array, and each `(1, y, x)`
  chunk is one PNG, decoded by `imagecodecs_png`.
- **Metadata.** Each well carries its `metadata.csv` fields (treatment,
  concentration, SMILES, condition, cell type). The whole CSV is also an
  Iceberg table keyed by `site_id`.
- **Embeddings.** Copied into a native float32 Zarr array per dataset, of
  shape (site, feature), with a `site_id` array.
- **Manifests.** Packed one per plate by ice-pack, a small Rust tool built for
  this ingest: 110 manifests instead of the 375,904 (one per array) that
  Icechunk 2.3 writes.

The figures draw the zips as published (`diagrams/source.svg`), each source
level beside the store nodes it becomes (`diagrams/mapping.svg`), and the
store with every node's attributes (`diagrams/store.svg`). `LAYOUTS.md`
records what the bucket holds and the source's errors.

**Decisions.** Each is argued under "Judgment calls", by number.

| # | Decision | Status |
|---|---|---|
| 1 | Virtual, not converted | Decided |
| 2 | One repo, the datasets as top-level groups | Proposed |
| 3 | Packed manifests, one per plate, written by ice-pack | Proposed |
| 4 | Well names read as row letter and column number, against the README | Proposed |
| 5 | RxRx19a's pixel size is 1.30 µm, twice the README's 0.65 | Proposed |
| 6 | RxRx19b's channels are named `w1` … `w6` | Proposed |
| 7 | Embeddings as a Zarr array, not a table | Decided |
| 8 | One resolution level, no overview levels | Proposed |
| 9 | Sites keep the source's numbers, 1 to 4 | Proposed |

The repo `earthmover-biology/RxRx19` exists, with a virtual chunk container
for `gs://rxrx/` backed by an anonymous bucket config, `rxrx`, and a public
virtual chunk access policy (set up 2026-11-11). **Approvals still needed**,
each before it runs: the one-plate trial on a branch, and the full ingest.

## The archive

| | RxRx19a | RxRx19b |
|---|---|---|
| What it images | SARS-CoV-2 infection in human renal cortical epithelial (HRCE) and Vero cells | HUVEC in a COVID-19 cytokine-storm model |
| Paper | [Heiser et al. 2020](https://doi.org/10.1101/2020.04.21.054387) | [Cuccarese et al. 2020](https://doi.org/10.1101/2020.08.02.233064) |
| Images zip | 477.7 GB, 1,527,600 PNGs | 438.3 GB, 422,304 PNGs |
| Experiments, plates | HRCE-1, HRCE-2, VERO-1, VERO-2; 57 plates | HUVEC-1; 53 plates |
| Wells, sites | 76,380 wells × 4 sites = 305,520 | 70,384 wells × 1 site |
| Channels | 5: Hoechst, ConA, Phalloidin, Syto14, WGA | 6, unmapped (decision 6) |
| PNGs | 1024 × 1024, 8-bit, downsampled from 2048 × 2048 16-bit | 2048 × 2048, 8-bit |
| Compounds | 1,669 | 1,856 |
| Embeddings | 1024 per site, 1.5 GB zip | 128 per site, 42.5 MB zip |

The bucket is public and read anonymously, over `gs://rxrx/` or
`https://storage.googleapis.com/rxrx/`. The license is CC BY 4.0 for both
([RxRx19a](https://www.rxrx.ai/rxrx19a), [RxRx19b](https://www.rxrx.ai/rxrx19b));
Recursion's other datasets on the bucket are not CC BY and are out of scope.
Neither dataset is in the Cell Painting Gallery. No public viewer of either
dataset's images was found; the community has used the metadata and
embeddings ([a Kaggle upload of RxRx19a's
embeddings](https://www.kaggle.com/datasets/tunguz/rxrx19a)).

## Proposed layout

### The spec's rules that shape it

- A plate imaged in a multi-well format is an HCS plate: plate, row, well,
  then one image per field of view. A field may not be stacked with others
  into one array, because an image allows only one axis that is neither space
  nor time.
- A plate declares every row and column of its physical format, and lists the
  wells that hold images. These are 1536-well plates, A–AF by 1–48; the wells
  with images are B–AE by 2–47.
- An image's axes are `c, y, x`, with units on the space axes.

### The tree

```
/                                     title, source, license, the two datasets
rxrx19a/                              title, citation, source URLs
rxrx19a/HRCE-1/                       cell type
rxrx19a/HRCE-1/Plate1/                HCS plate: 32 rows × 48 columns, 1,340 wells, field_count 4
rxrx19a/HRCE-1/Plate1/AA/             row (no attributes)
rxrx19a/HRCE-1/Plate1/AA/2/           well: its sites; treatment, concentration, SMILES, condition
rxrx19a/HRCE-1/Plate1/AA/2/2/         site: OME-Zarr image, c, y, x
rxrx19a/HRCE-1/Plate1/AA/2/2/0        array (5, 1024, 1024) uint8, chunks (1, 1024, 1024), imagecodecs_png
rxrx19a/embeddings                    array (305,520, 1024) float32, native
rxrx19a/site_id                       array (305,520) string, the embeddings' rows
rxrx19b/…                             the same, 53 plates of 1,328 wells × 1 site, (6, 2048, 2048) arrays
```

`diagrams/store.svg` shows every level's attributes with one real site's
values (RxRx19a HRCE-1 Plate1, well AA02, site 2).

### Referenced, written, and kept outside Zarr

| | how | size |
|---|---|---|
| PNGs | referenced in place, one chunk each | 1,949,904 references, 916 GB of source |
| Groups and attributes | written | 901,990 nodes |
| Embeddings | copied, native float32, blosc zstd | 1.29 GB before compression |
| `metadata.csv` | an Iceberg table per dataset, keyed by `site_id` | 375,904 rows |

### Layouts considered and rejected

- **Converting to native chunks** (one chunk per site holding all channels,
  with overview levels): about 916 GB to store indefinitely, for one GET per
  site instead of 5 or 6 and faster decoding. It keeps the same node count.
- **A table of byte ranges only** (`site_id` → `url, offset, length` per
  channel): no copy and no snapshot, but no viewer can open it.
- **Two repos, one per dataset**: smaller snapshots, but the Cell Painting
  Gallery ingest keeps its datasets as top-level groups of one repo, and the
  snapshot's size is a known limit of Icechunk 2.x, not a reason to split a
  collection (decision 2).

## What has been tested

All on 2026-11-11, from a laptop, with icechunk 2.3.0.

- **The central directories** were read in full: 1,527,665 members (190.5 MB
  of directory, 19 s) and 422,362 members (53.2 MB, 3.5 s). Every PNG is
  stored; only each zip's LICENSE and README.md are deflated.
- **The 28-byte rule** holds for every member of both zips: each member's span,
  from its local header to the next, equals `30 + name + 28 + size`. 200 local
  headers per zip, read directly, all carry a 28-byte extra field.
- **The metadata joins** one to one: every `metadata.csv` row has a site with
  every channel's PNG, and every PNG's site has a row.
- **One site, end to end.** HRCE-1 Plate1 AA02 site 2 was written to a local
  repo in this layout and read back through Icechunk from `gs://rxrx`. Channel
  `w3` equals the PNG decoded straight from the zip, pixel for pixel.
- **Snapshot size.** Two RxRx19a plates and one RxRx19b plate were written to a
  local repo, a commit each. A node costs 62.2 bytes of snapshot in RxRx19a
  and 66.2 in RxRx19b, so the whole store's snapshot is about 57 MB for
  901,990 nodes. Unpacked, the manifests were 298 and 313 bytes per array.
- **ice-pack** packed 24 arrays of 5 references into 2 manifests in a local
  repo; a fresh Icechunk 2.3.0 Python reader read back all 120 chunks
  correctly, and a run whose branch moved before its commit stopped without
  committing.
- **Embeddings.** Every value of the rows checked prints back identically from
  float32.

Not yet measured: a commit and a chunk read against the real object store,
ice-pack against S3, and the time a reader takes to open the 57 MB snapshot.

## Judgment calls

**1. Virtual, not converted.** A Zarr store can only show these PNGs as
OME-Zarr images by referencing them in place, which needs Icechunk, or by
copying 916 GB into new chunks. The copy would buy one GET per site instead of
5 or 6, and a faster decode, for an image set whose RxRx19a half is already an
8-bit downsampled derivative. Recommendation: virtual.

**2. One repo, `earthmover-biology/RxRx19`, with `rxrx19a/` and `rxrx19b/` as
top-level groups.** A reader browses both screens from one root, as the Cell
Painting Gallery ingest does with its datasets. The cost is the snapshot:
about 902,000 nodes and 57 MB, which every reader fetches on open and which is
above Icechunk's default snapshot cache of 500,000 nodes. RxRx19a alone is
689,000 nodes, so splitting would not bring either repo under the cache.
Recommendation: one repo, with the snapshot size stated as a known risk.

**3. Packed manifests.** Icechunk 2.3 writes one manifest per array, and each
site's array holds 5 or 6 references, so a plain commit writes 375,904
manifests of about 300 bytes, 5,360 PUTs per RxRx19a plate, and a reader of a
whole plate makes 5,360 manifest GETs before any chunk. The manifest format
already allows one manifest to hold many arrays; nothing in Icechunk's API
writes one. ice-pack does, at write time: the ingest commits the groups and
empty arrays through a normal session, writes each batch's references to a
Parquet file, and ice-pack commits them in one manifest per plate. A later
ordinary commit to one array moves that array to its own manifest and leaves
the rest packed. ice-pack is meant to be short-lived, until Icechunk can group
manifests itself. Recommendation: pack, one manifest per plate.

**4. Well names.** `AA02` is row AA, column 2. Both READMEs say "column AA,
row 2", but the letters run B–AE (30) and the numbers 02–47 (46), which fit a
1536-well plate's 32 rows and 48 columns only one way. The store follows the
data and keeps the source's well name in each well's `well_id`.

**5. RxRx19a's pixel size.** Both datasets' READMEs in
`recursionpharma/rxrx-datasets` say "Physical resolution: 0.65 micron/pixel".
RxRx19b's PNGs are full-size, so 0.65 µm. RxRx19a's PNGs are 2048-pixel images
downsampled to 1024, so if 0.65 µm describes the instrument, as it does for
RxRx19b, the PNGs' pixels are 1.30 µm. The README does not say, and RxRx1's
README gives the same 0.65 for 512-pixel images. Recommendation: 1.30 µm, with
the reasoning in the dataset's attributes; ask Recursion, or check against the
paper's methods, before the full ingest.

**6. RxRx19b's channel names.** RxRx19a's README maps `w1` … `w5` to stains;
RxRx19b's maps none. Its dataset page lists the same five stains plus
MitoTracker, which suggests `w1` … `w5` as in RxRx19a and `w6` MitoTracker,
but nothing states it. Recommendation: name them `w1` … `w6`, and record the
likely mapping as unconfirmed in the dataset's attributes.

**7. Embeddings as a Zarr array.** The embeddings ship as a CSV beside
`metadata.csv`, but they are a sites × features matrix of floats; readers slice
blocks of rows and do linear algebra on them. A (site, feature) array does
that directly; an Iceberg table would need 1024 float columns or a list
column. `metadata.csv`, with mixed fields queried by treatment or condition,
stays a table. Decided.

**8. One resolution level.** An overview level would need every PNG decoded
once, 916 GB, and written natively. A plate view in a viewer loads every
site's full image without one. Recommendation: no overview levels now; build
them later as their own commit if plate views turn out to matter.

**9. Site names.** Sites keep the source's numbers, 1 to 4, as in the Cell
Painting Gallery ingest, rather than renumbering from 0.

## Steps, scope and risk

1. **Code and fixture.** `ingest.py` in this directory: groups and arrays
   through a normal session, references to Parquet, ice-pack to commit them;
   embeddings and the Iceberg tables. A test runs it end to end on a small
   fixture zip built in the test.
2. **Trial, needs approval.** Write one plate of each dataset to a branch of
   `earthmover-biology/RxRx19`. Measure: the commit's time, ice-pack against S3, a chunk read from
   the Arraylake region, the snapshot's open time, and how a viewer shows a
   plate.
3. **Full ingest, needs approval.** RxRx19a first, its 57 plates and its
   embeddings, then RxRx19b's 53 plates: one batch script per dataset under
   `runs/`, on Coiled. Building the plates reads no image data; the
   embeddings copy reads 1.5 GB. RxRx19a goes first because it is the one
   people will want to look at: its stains are named, and its infected wells
   sit beside mock and UV-inactivated controls on every plate. It is also
   the larger snapshot, about 689,000 nodes, so its run measures the
   snapshot's open time on the real store.
4. **Verify and report.** `verify.py` checks the node and reference counts
   against the central directories, the attributes against the schemas, and a
   handful of chunks against the PNGs decoded straight from the zips;
   `REPORT.md`; a notebook that opens the store cold.

Out of scope: Recursion's RxRx1, RxRx2 and RxRx3 (other licenses); the loose
copy of RxRx19a's images on the bucket (incomplete; `LAYOUTS.md`); overview
levels (decision 8).

**Risks.**

- The snapshot, about 57 MB and 902,000 nodes, makes opening the repo slow;
  the trial measures how slow.
- Reads cross clouds: the chunks are on Google Cloud, Arraylake readers are
  usually on AWS. The bucket is not requester-pays (it reads anonymously), so Recursion pays the egress of every chunk read.
- ice-pack is new code that writes snapshots directly; a bug there could write
  a snapshot stock Icechunk misreads. The trial reads every chunk of the trial
  plates back through stock Icechunk before the full ingest.
- If Recursion rewrites a zip, every reference into it goes stale. Each
  reference can carry the zip's ETag, so Icechunk refuses a changed zip
  rather than returning wrong bytes.
