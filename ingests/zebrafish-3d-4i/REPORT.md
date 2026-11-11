# 3D-4i zebrafish embryos (S-BIAD3228): ingest report

## What was built

The 3D-4i zebrafish plate from the BioImage Archive study
[S-BIAD3228](https://www.ebi.ac.uk/biostudies/bioimages/studies/S-BIAD3228)
is in the Arraylake repo `earthmover-biology/zebrafish-3d-4i`, as a native
OME-Zarr 0.6 HCS plate in Icechunk. The source is one 591 GB zip of an
OME-Zarr 0.4 plate in Zarr v2, written by Fractal and served by EBI. The zip
Deflate-compresses every member, so its chunks cannot be referenced in place.
This store is therefore a copy: every pixel was decoded from the zip and
written again.

- **Snapshot.** `YFEF3GGBZ2N59YY6T8EG` on `main`, tagged `source-copy`.
- **Contents.** All 30 wells and all 211 fields of view, one image per field.
  Every pixel value and every piece of source metadata is the authors',
  including the source's channel colors and its coarse pyramid levels.
- **Access.** Any logged-in Arraylake account can read the repo.
- **Layout.**

  ```
  /                                    OME-Zarr 0.6 HCS plate, AssayPlate_Greiner_#655090;
                                       the study, the zip, the preprint and the license
  <row>/<column>/                      well: its images, and a scene placing them on the stage
  <row>/<column>/<n - 1>/              field FOV_n: one embryo, all four rounds
    0/ 1/ 2/ 3/                        pyramid levels, (c, z, y, x) uint16, (16, 271, 1000, 1000) at level 0
    labels/<name>/                     nucleiRaw, cells, cyto, membrane, embryoRaw, Pol-II-S2P_foci,
                                       from round 1, cut to the field
  ```

  Channel `4 * r + c` of each image is channel `c` of round `r`, with its
  source name, color, window and `wavelength_id`. Pixels are 0.65 µm in y and
  x and 1 µm in z at level 0. Levels 1 to 3 are the source's own coarse
  pixels. Each covers every coarse pixel its field touches, so it can be one
  pixel wider than 1000 / 2^k, and its translation records where it sits.
  The three ROI tables are kept as attributes on the wells and images.
  LAYOUTS.md maps every source key onto the store.
- **Figures.** Written by `diagrams.py` from the store and the zip's central directory:
  - [the source zip](diagrams/source.svg)
  - [the mapping of one field](diagrams/mapping.svg)
  - [the store's hierarchy](diagrams/store.svg)
- **Chunks.** Image chunks are one plane of one channel, in 32-plane shards.
  Label chunks are one plane, in one shard that spans all 271 planes. Both
  use blosc zstd level 5 with bitshuffle.
- **Opening it.** The repo opens with the `arraylake` client and zarr-python.
  The plate's `well` metadata lists each well's images, and each well's
  `scene` lays its fields out in one `well` coordinate system.

### How the copy works

`ingest.py` reads the zip once, well by well, in the order the zip stores its
bytes. Each well is one contiguous region of the zip that holds every
metadata document the well needs. The zip's central directory, read once and
kept as a Parquet file, is the only index.

- **Fetch.** A well's region is read as 64 MiB range requests. A request
  still out after 20 s is made a second time.
- **Check.** Every member's bytes are checked against the CRC-32 in the
  central directory. A member that fails is read again, so damaged bytes
  cannot become pixels.
- **Decode.** Each channel of each source array is decoded once all of it
  has arrived, and each field's window is copied into that field's buffer.
- **Write.** Each field's channel is written through zarr. A well commits
  every 5 minutes, and its HCS metadata goes in its last commit, so a rerun
  resumes after the work already committed.
- **Overlap.** Fetching, decoding and uploading run on separate threads.
  Each waits only when the bytes it has handed on reach a limit: 8 GB
  compressed and 16 GB decoded.
- **Stalls.** If nothing moves for 60 s, every thread's stack is printed with
  the tail of Icechunk's log.

### Runs

Every run was a Coiled batch job on an m6i.4xlarge (16 cores, 61 GB) in AWS
London (eu-west-2). Each read the zip from EBI over HTTPS, with 16 range
requests in flight, 8 upload workers and a zarr concurrency of 32. Each wrote
to the branch `trial-stream`, and `main` was fast-forwarded to it on
2026-10-29.

| Run | Wells | Result |
|---|---|---|
| [`runs/01_e04_g05_d06.sh`](runs/01_e04_g05_d06.sh), one VM | E/04, G/05, D/06 | E/04 and G/05 committed; D/06 failed on a member that did not inflate |
| [`runs/02_remaining_wells.sh`](runs/02_remaining_wells.sh), four VMs in one hour | the other 27, and D/06 resumed, seven per VM | all committed |

The rate is quoted in seconds per GB of zip read, because the labels
compress to almost nothing and raw pixel bytes say little.

- **E/04 and G/05:** 18.0 and 17.5 s per zip GB.
- **The 27 wells of the full run, D/06 aside:** 272 to 843 s each, 16.4 to
  50.9 s per zip GB, median 22.7. D/06 is left out because it resumed with
  41 channel runs already committed.
- **Bottleneck:** uploads, while EBI kept up. In each well of the full run,
  decoding waited on uploads for 95 to 381 s, and the fetch waited on
  decoding for 0 to 114 s.
- **Peak memory:** 48.5 GB.

### Lessons for a similar copy

These held for this plate, and apply to any large zip read over HTTP from a
public archive.

- **EBI has slow spells that a second request does not escape.** Single
  64 MiB ranges took from 53 to 309 s on D/06, and a request made a second
  time was just as slow. Keeping 64 ranges (4 GiB) asked for ahead rode out
  about 30 s of it. A longer spell stalls the copy for minutes whatever the
  converter does, so plan the run's time with it.
- **Expect damage in transfer, and check every member.** Three members in 30
  wells failed their CRC-32 check on the first read and were good on the
  second. Without the check those bytes would have been decoded into pixels
  or failed as a corrupt source.
- **Uploads, not decoding, set the pace.** Decoding waited on uploads for
  minutes in every well. The converter kept 10 to 26 shard writes in flight,
  while a separate test reached 522 MB/s to R2 with 128 in flight. The next
  copy should keep about 32 in flight.
- **Watch for stalls from a thread of their own.** The converter prints every
  thread's stack after 60 s without progress. Of 11 such dumps in the full
  run, 10 showed only the fetch waiting on EBI, and every stall cleared by
  itself. A suspected deadlock in creating arrays never reproduced, so the
  pause added for it was removed.
- **Measure in seconds per GB of zip read.** Labels compress to almost
  nothing, so raw pixel bytes per second varies by well for no reason that
  matters.
- **Keep the central directory.** It is 179 MB for two million members, and
  can take many minutes to read from EBI. The converter keeps it as Parquet,
  and every job and `verify.py` read that copy. A member read checks its own
  local header and CRC, so a stale copy fails loudly.
- **Commit a well in pieces.** Each well commits every 5 minutes, and a rerun
  resumes after the channel runs committed, which is how D/06 picked up after
  its failure.

## Verification

`verify.py` ran from a laptop in California against `main` at
`YFEF3GGBZ2N59YY6T8EG` on 2026-11-11. It passed in 60 s.

- **`complete`** passed in 0.1 s. Every well, round and label that the zip's
  central directory names is in the store. The directory is the converter's
  Parquet copy, so this check makes no request to EBI.
- **`schema`** passed in 5.8 s. Every OME-Zarr document validates against the
  0.6 schema.
- **`pixels`** passed in 51 s. At level 0, plane 135, it compared two images
  against the source zip read from EBI: D/06/6 in channel Pol-II-S2P and
  G/05/2 in channel PCNA_restained, each with all 6 of its labels. The
  field's window in the source comes from the ROI row and pixel size the
  store records, so a wrong ROI or pixel size would fail this check too.

Three other checks cover what the store check does not:

- **During the run,** every source member was checked against the zip's
  CRC-32, and three members that failed were read again (under "Problems
  found in the source data"). Before each well's commit, the converter
  checked that every round's `FOV_ROI_table` agrees, that each pyramid level
  is the one above halved, and that every array's `.zarray` matches the
  encoding used to decode it.
- **The 18 tests in `test_ingest.py`** check the conversion end to end on a
  scaled-down plate.
- **On 2026-11-02, by hand,** the multiscales of the store were checked
  against the source and are correct. On B/02/0 and E/04/3, plane 135, the
  `cells` label at levels 1 to 3 equals the bottom-right pixel of each
  level-0 block exactly. That places each coarse label level (2^k − 1)/2
  level-0 pixels from where its translation says (PLAN.md decision 17).

## Changes from the plan

PLAN.md predates the full run. Where it and the run disagree, the run logs
are the record.

- **The conversion read EBI, not a staged copy.** Decisions 1 and 2 staged
  the zip in R2 so that every conversion would read it from there. Every
  conversion run read the zip from EBI over HTTPS instead.
- **The converter overlaps its stages with byte limits.** Decision 14 queued
  at most two channel runs ahead and committed once per well. The converter
  holds up to 8 GB fetched and 16 GB decoded, repeats a range request that
  is slow, commits every 5 minutes, and resumes a well after a failure.
- **The time per well is set by uploads.** The plan modeled about 2 minutes
  a well, limited by CPU. Wells took 272 to 843 s, and uploads set the pace.
- **Peak memory was higher.** The plan modeled 24 to 29 GB; a job reached
  48.5 GB on its 61 GB VM.
- **The step 3 comparison was not run.** The plan built one well three ways
  (16 channels per embryo, one image per round, 10-plane shards) and timed
  real reads before the full run. The full run used the layout of decisions
  3 and 5 as decided.
- **Verification is lighter.** Step 5 checked pixels on a sample from every
  well, round and level, with levels 1 to 3 compared exactly. The first
  `verify.py`, run on 2026-10-29, hung for 2 hours reading the source plate's
  metadata from EBI and was stopped. `verify.py` now runs three checks in
  about a minute, and levels 1 to 3 were checked by hand.

## Judgment calls made

PLAN.md argues each call by number. The copy follows decisions 3 to 7 and
11 to 14 as decided.

| # | Call | As built |
|---|---|---|
| 3 | Plate layout | One image per embryo, holding all 16 channels of its four rounds; field `FOV_n` is image `n - 1` |
| 4 | Round 3 | In every image, with channel names from its own well's `omero` metadata. The plate declares no acquisitions; its attributes list each round's channels, round 3 per well, and the source's own acquisitions |
| 5 | Chunks and shards | Image planes in 32-plane shards; label planes in full-depth shards |
| 6 | Codec | blosc zstd level 5 with bitshuffle |
| 7 | ROI tables | Folded into the layout: each `FOV_ROI_table` row is an image and its scene translation; all three tables' values kept as attributes; no Iceberg tables |
| 11 | OME-Zarr version | 0.6, so each well's `scene` can place its fields on the stage |
| 12 | Pyramid levels 1 to 3 | The source's coarse pixels, unchanged, with a translation per level |
| 13 | Damaged source chunks | Read as the fill value, 0, and listed on the image whose field they fall in, under `annotations.empty_source_chunks` and `annotations.unreadable_source_chunks` |
| 14 | Reading the zip | Each well read once, in file order, as 64 MiB ranges |
| 15 | Improvements after the copy | `main` at `source-copy` holds the copy alone; each improvement will be its own commit on top |

## Problems found in the source data

LAYOUTS.md, "What the source taught us", has every item in full.

- **Round 3 is missing from the HCS metadata.** Every well holds four
  rounds, but the plate lists acquisitions 0 to 2 and each well lists images
  0 to 2. A reader that follows the metadata never finds round 3.
- **Round 3 stains different markers in different wells.** In 21 wells it
  stains Nanog, YAP and XRN2. In 9 it restains three markers from an earlier
  round. Each image's `omero` channel list is the only record of which.
- **1,244 chunk members are empty files.** Every well has some, from 14 to
  94. 139 of them are level-0 image chunks inside embryos, 479 are image
  chunks at levels 1 to 3, and 626 are label chunks. No reader can decode an
  empty chunk.
- **Some chunks are cut short.** `C/02/1/labels/Pol-II-S2P_foci/1/6/0/3`
  holds 16,384 bytes of a Blosc frame whose header records 162,569. Each one
  inside a field is listed in its image's
  `annotations.unreadable_source_chunks`.
- **Three members failed their CRC check on the first read** and were good
  on the second: `E/06/0/0/2/21/3/11`, `G/02/0/0/3/25/5/15` and
  `D/07/1/labels/cells/0/11/0/11`. That points to damage in transfer rather
  than in the archive. D/06's failed member in the first run was likely the
  same: on the rerun, D/06 completed.
- **`FOV_ROI_table` rows are in no fixed order,** and the order differs
  between rounds. The values agree field by field.
- **`x_micrometer_original` is about half of `x_micrometer`** in every row,
  and likewise for y. Both are kept; neither is explained.
- **Every embryo carries label 1 in `embryoRaw`,** so `embryoRaw_ROI_table`
  has one row per well whose box spans the well.

## Upstream issues hit

[docs/upstream.md](../../docs/upstream.md) has the details of each.

- **Raw Deflate.** imagecodecs cannot decode a raw Deflate stream without its
  uncompressed size, so the zip parser takes only STORED members as virtual
  chunks. This zip Deflates every member, which is one reason this ingest
  copies.
- **Decoding a chunk from bytes.** zarr has no public call that decodes a
  chunk's bytes with an array's metadata, so `minus80.parsers.zip.zarr_v2`
  decodes streamed members with numcodecs, from each array's `.zarray`.
- **Merged range requests.** obstore's `get_ranges` merges adjacent ranges
  into one request, so `minus80.core.fetch.iter_ranges` issues one
  `get_range` per range
  ([obstore#678](https://github.com/developmentseed/obstore/issues/678)).
- **Short reads.** obstore returns a short body without complaint, so
  `minus80.core.fetch` checks that each range returned every byte asked for.
  No issue found.
- **The zip's end record.** `minus80.parsers.zip.structures` reads it with
  the private `zipfile._EndRecData`. Not filed.
- **An inflating zip store.** `verify.py` reads the source through
  `minus80.parsers.zip.store.MemberStore`, because neither VirtualiZarr nor
  zarr has a public, concurrent zip store that inflates Deflate members.
  Not filed.

## Next

- **Improvement commits.** PLAN.md decisions 15 to 19 add each change as its
  own commit on `main`, described in its own metadata:
  - 16, distinct channel colors within each round;
  - 17, corrected translations for the coarse label levels;
  - 18, a `missing_source` label image marking the damaged source chunks;
  - 19, levels 1 to 3 rebuilt from level 0, with full-depth shards at the
    coarsest levels.

  The branch `enhance` on Arraylake holds one draft commit, which is not on
  `main`. A tag follows decision 19.
- **The repo's description** on Arraylake is empty.
- **A report to the authors and the BioImage Archive** of the missing round
  3 and the 1,244 empty members.
