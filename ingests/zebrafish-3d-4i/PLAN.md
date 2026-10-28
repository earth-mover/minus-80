# 3D-4i zebrafish embryos (BioImage Archive S-BIAD3228): ingest plan

## Summary

The plan brings the 3D-4i zebrafish embryo dataset into Icechunk as a native
OME-Zarr 0.6 HCS plate, by way of a staged copy of the source zip in our R2
bucket.

The dataset ([S-BIAD3228](https://www.ebi.ac.uk/biostudies/bioimages/studies/S-BIAD3228),
CC0) is whole-mount, four-round multiplexed immunofluorescence of 212 early
zebrafish embryos from the Pelkmans lab ([Hess et al.
2025](https://www.biorxiv.org/content/10.1101/2025.10.27.684723v1)). It is
published as one 591 GB zip of an OME-Zarr 0.4 plate, served by EBI over
HTTPS, FTP, Aspera and Globus. Every member of the zip is Deflate-compressed,
and no copy of the data sits in an object store. Both facts rule out
referencing the archive's bytes in place from Arraylake, so this ingest is a
copy.

- **Stage.** Copy the zip, unchanged, from EBI to R2 once, from a VM in AWS
  London. From there EBI serves 170 to 380 MB/s over parallel HTTPS, so the
  copy takes about an hour; from a laptop in the United States it managed
  1.5 MB/s. Every later step reads from R2.
- **Convert.** Read the staged zip on Coiled and write a native Icechunk store:
  an OME-Zarr 0.6 plate with one image per embryo (field of view) holding all
  16 channels of its four rounds, blosc zstd
  with bitshuffle, sharded so that a single z-plane is one small read. Cutting
  each field out of its well image leaves the filler behind: the all-zero
  padding chunks, 57% of the zip's members, are never read.
- **ROI tables.** The source's three AnnData ROI tables become structure: each field becomes its own image, positioned in its well, and the tables' values are kept as attributes.

**Decisions.** Each is argued in full under "Judgment calls", by number.

| # | Decision | Context | Status |
|---|---|---|---|
| 1 | Stage the zip in R2 before converting | Any rerun of the conversion reads R2, where egress is free, instead of pulling 591 GB from EBI again | Decided |
| 2 | Transfer over parallel HTTPS from AWS London | 8 connections into a resumable multipart upload, with backoff and an MD5 check on every part; EBI's Aspera and Globus routes need credentials or an endpoint we do not have | Decided; the transfer itself awaits approval |
| 3 | One image per embryo, with every round's channels on its channel axis | The source pastes a well's 5 to 9 embryo photos onto one well-sized image per round, 96% zeros, with a chunk grid that cuts across the embryos. The HCS spec says a well's images are its fields of view. Rounds cannot be an axis of their own, because an image allows only one axis that is neither space nor time; the published rounds are already registered, so they share one image as 16 channels. Field `FOV_n` is image `n - 1` | Decided 2026-10-27; the trial also builds one image per field and round, on a branch, for comparison |
| 4 | Include round 3 | Every well holds four rounds (0 to 3), but the plate lists acquisitions 0 to 2 and each well lists images 0 to 2, so HCS readers never find round 3 (DAPI_3, Nanog, YAP, XRN2). Every image holds all four rounds' channels, and the plate's attributes record each round's channels | Decided; telling the authors is optional |
| 5 | Chunks are whole xy planes, shards run along z | Images: `1 × 1 × 1000 × 1000` chunks in 32-plane shards. Labels: `1 × 1000 × 1000` chunks in full-depth shards | Decided; step 3 times 32- against 10-plane shards |
| 6 | blosc zstd level 5 with bitshuffle | Decodes at about 5.8 GB/s; pcodec compresses field pixels 11 to 21% smaller but decodes at 1.4 GB/s, and on fast connections decode speed counts for more. blosc is a codec the Zarr v3 core spec defines; pcodec is not | Decided |
| 7 | Fold the ROI tables into the layout | Each `FOV_ROI_table` row becomes an image placed by a 0.6 translation; the other two tables are kept as well attributes; no Iceberg tables | Decided |
| 8 | No virtual store over the staged zip | It would need a custom codec and be replaced by the native store | Decided |
| 9 | Delete the staged zip once the store is verified | EBI remains the archive of record | Decided |
| 10 | Destination | Repo `earthmover-biology/zebrafish-3d-4i` on Arraylake managed storage; the zip at `files/ingest-staging/` in the same bucket | Decided 2026-10-27 |
| 11 | OME-Zarr 0.6 | 0.6 scenes place each field image in its well | Decided 2026-10-27 |
| 12 | Copy pyramid levels 1 to 3 from the source, rebuild later | Most field origins fall between coarse pixels, so each field's copied level is a pixel larger than half, a quarter or an eighth of 1000, with a per-level offset; a later commit rebuilds the levels from level 0 | Decided |

**Settled by the step 3 trial, by measurement:**

- whether 32-plane shards beat 10-plane shards on real reads, in-region and
  from London;
- whether one 16-channel image per embryo, or one image per embryo and round,
  suits the people who use these data; the trial builds both on separate
  Icechunk branches, for a domain expert to open side by side;
- whether one group can carry both the HCS `well` metadata and a 0.6 `scene`;
- whether viewers apply the scene's translations and the per-level offsets;
- what each field's `x_micrometer_original` means; it is about half its
  `x_micrometer`.

**Approvals still needed**, each before it runs: the transfer of the zip,
creating the repo for the step 3 trial, and the full conversion.

## The archive

**The experiment.** Embryos were fixed every 15 minutes from 2 to 4 hours
post-fertilization (cell cycles 7 to 12, during zygotic genome activation),
deyolked, mounted in wells (the field-of-view tables hold 5 to 9 per well; the preprint says 7 to 9), and imaged on a Yokogawa CV8000 spinning-disk
confocal with a 20×/1.0 NA water objective. Each well was stained, imaged and
eluted four times. Every round images DAPI plus three markers:

| Round | Channels (source names) |
|---|---|
| 0 | DAPI_0, Pol-II-S2P, FLAG, PCNA |
| 1 | DAPI_1, H3K27Ac, bCatenin, pH3 |
| 2 | DAPI_2, H2B, ALYREF, Pol-II-S5P |
| 3 | DAPI_3, Nanog, YAP, XRN2 |

FLAG reports Pou5f3 in a `pou5f3-3xflag` line. YAP and XRN2 are not in the
preprint's ten-plex panel.

**What is in the zip.** Surveyed 2026-10-27 by reading the zip's whole
central directory (2,036,714 members) and all 8,648 metadata documents.

- **One plate**, `AssayPlate_Greiner_#655090`, OME-Zarr 0.4 HCS in Zarr v2,
  written by Fractal. 30 wells in rows B to G, columns 02 to 07.
- **Four images per well**, one per round, each `(c, z, y, x)` = `(4, 271, y,
  x)` uint16, with a well extent that differs in every well: y from 11,282 to 16,816
  pixels and x from 13,026 to 18,039, the same in all four rounds of a well. Pixels are 0.65 µm in y and x, 1 µm in z. Four pyramid levels,
  downsampled 2× in y and x each time.
- **Each image is a well-sized image holding 5 to 9 fields of view (211 in all).** The
  `FOV_ROI_table` places each 650 × 650 µm field (1000 × 1000 pixels) at
  its stage position. Level 0 stores only the chunks the fields touch:
  324,352 of the 3,144,512 chunks its grid allows.
- **Segmentations** on round 1 of every well: `nucleiRaw`, `cells`, `cyto`,
  `membrane`, `embryoRaw` and `Pol-II-S2P_foci`, uint32, on the same grid as
  the images.
- **AnnData ROI tables** on every image: `well_ROI_table` and `FOV_ROI_table`,
  plus `embryoRaw_ROI_table` on round 1.

| | Chunks | Deflated in the zip | blosc-lz4 frames |
|---|---|---|---|
| Image chunks | 1,474,368 | 570.1 GB | 910.5 GB |
| Label chunks | 552,888 | 20.8 GB | 118.0 GB |
| of which all-zero padding | 1,155,683 | 0.85 GB | |
| Metadata and table members | 9,458 | | |

The non-empty image chunks hold about 13.6 TB of raw uint16 pixels.

**How the source is packed.** Each chunk is `1 × 10 × 1000 × 1000`
(channel, z, y, x), compressed with blosc-lz4, and then compressed again by the
zip with Deflate. The second pass shrinks the blosc frames by about 40%,
because lz4 compresses weakly.

**How people use it.** The preprint's analysis segments nuclei and cells,
measures per-object intensities across all rounds and aggregates them per
embryo. Rounds are aligned on DAPI with elastix, using round 1 as the
reference. That alignment is already applied to the published images (see
"What has been tested").

## Proposed layout

```
zebrafish-3d-4i/                     OME-Zarr 0.6 HCS plate, AssayPlate_Greiner_#655090
  B/02/                              well: one image per embryo, and a scene placing them
    0/                               field FOV_1, all four rounds
      0/ 1/ 2/ 3/                    pyramid levels, (c, z, y, x) uint16, (16, 271, 1000, 1000) at
                                     level 0; levels 1 to 3 copied from the source (judgment call 12)
                                     c = DAPI_0, Pol-II-S2P, FLAG, PCNA,      round 0
                                         DAPI_1, H3K27Ac, bCatenin, pH3,      round 1
                                         DAPI_2, H2B, ALYREF, Pol-II-S5P,     round 2
                                         DAPI_3, Nanog, YAP, XRN2             round 3
      labels/                        nucleiRaw, cells, cyto, membrane, embryoRaw, Pol-II-S2P_foci,
                                     from round 1, cut to this field
    1/                               field FOV_2
    ...                              5 to 9 embryos per well, 211 images in all
  B/05/ ...                          30 wells
```

- **Axes and units** come from the source: `c` channel; `z`, `y`, `x` in
  micrometers; scale `[1, 1, 0.65, 0.65]` at level 0, doubling in y and x per
  level, plus a translation at levels 1 to 3 (judgment call 12). Channel names, colors and display windows come from the source's
  `omero` block, in the source's order.
- **One image per embryo** (judgment call 3). Each image is the field's
  1000 × 1000 pixels cut from the source well image, every channel of every
  round in round order. Field `FOV_n` is image `n - 1`. Each channel keeps its
  source name, display color, window and `wavelength_id`, and the image's
  annotations record each channel's round. The well's scene translates each
  image to its field's position on the stage, in micrometers, from the
  `FOV_ROI_table`, so a viewer can lay a well out as the source did.
- **The plate** declares no acquisitions, because each image holds all four
  rounds. Its attributes record each round's channels, round 3 included, and
  the acquisitions the source's own plate listed (judgment call 4).
- **Chunks and shards** (judgment call 5), as (c, z, y, x):
  - **Images:** chunks of `1 × 1 × 1000 × 1000`, one whole plane of one
    channel of one field, in shards of `1 × 32 × 1000 × 1000`, about 15 MB.
    Showing one plane fetches the shard index and one chunk, about 0.5 MB; an
    embryo volume fetches 9 to 10 whole shards in parallel.
    At levels 1 to 3 the chunk is that level's whole plane, in the same
    32-plane shards.
  - **Labels:** chunks of `1 × 1000 × 1000`, in shards of `271 × 1000 × 1000` that
    span every plane. A label plane compresses to about 6 KB, so a 10-plane
    shard would make a label volume cost 28 requests per field for almost no
    data; a full-depth shard is about 1.6 MB and costs one.
- **Codec:** blosc zstd level 5 with bitshuffle (judgment call 6).
- **Empty chunks:** within a field almost nothing is empty. Image planes hold
  camera noise (values 0 to 8) even outside the embryo, and only label planes
  above and below the embryo are all zero: 6 of 271 for `nucleiRaw` in one
  C/02 field. zarr skips writing an all-zero chunk, and it reads back as the
  fill value, 0.
- **Root attributes** name the source study, the zip, the preprint and the
  license, so the store can be traced without this document.

**ROI tables, folded into the layout** (judgment call 7). The source's three
AnnData tables become structure and attributes rather than tables:

| Source table | Holds | In the store |
|---|---|---|
| `FOV_ROI_table` | each field's rectangle on the well image, 5 to 9 rows per well | one image per row; its position becomes the image's translation in the well's scene |
| `well_ROI_table` | one row: the well image's extent | a provenance attribute on the well group |
| `embryoRaw_ROI_table` | one row per well: the bounding box of label 1 in `embryoRaw`, which marks every embryo in the well with the same value, so the box spans the well | kept as a well attribute, so nothing the source said is lost; per field, the cut `embryoRaw` label shows where the embryo is |

Each field image keeps, as attributes, the field's source name (`FOV_1` to
`FOV_9`) and both source positions, `x/y_micrometer` and
`x/y_micrometer_original`.

**Layouts considered and rejected.**

- **Reference the zip in place.** Each chunk would need a custom codec that
  inflates raw Deflate, and Arraylake cannot hold virtual references to an
  HTTPS-only host. The staged copy in R2 fixes the second problem, which is why
  a virtual store remains an option (judgment call 8).
- **Copy the blosc-lz4 frames unchanged.** No pixel decode, but the store
  would be about 1.03 TB, nearly twice the zip.
- **The source's well image, one image per round.** Each well image is about 96%
  filler (well C/02 at level 3), and the chunk grid cuts across the fields, so
  reading one embryo reads two to four partly empty grid cells. In the
  well-image layout an embryo volume takes about 2.0 s from London even in
  32-plane shards, against 1.3 s per field (judgment call 3).
- **pcodec.** On field pixels it compresses 11 to 21% smaller than blosc
  zstd 5 with bitshuffle, but decodes four times slower (judgment call 6).
- **Other chunk and shard shapes.** Each layout was costed for six reads
  (one plane, a whole well at level 3, an xz slice, an embryo volume at
  levels 0 and 1, all 16 channels of an embryo, and a training patch), from
  in-region and EU readers, by simulating zarr-python's requests on measured
  chunk sizes and request times. Small single-plane chunks without shards
  make volumes and slices 11 to 22 times slower and write 18.8 million
  objects. Shards of `20 × 2000 × 2000` on the well image fetch parts of
  neighbouring fields: 448 MB for one embryo volume instead of 244 MB. 10-plane
  shards need three times the requests of 32-plane shards for a volume;
  full-depth shards turn a volume into one 128 MB request on one connection.
  Blocks of `8 × 250 × 250` match 32-plane shards for volumes and fetch seven
  times the bytes to show a plane.

## What has been tested

All measurements were taken on 2026-10-27. Unless the row says otherwise, they
ran on a laptop in the United States.

| Test | Result |
|---|---|
| EBI throughput from AWS London (`eu-west-2`, m6i.2xlarge), 64 MB ranges | 1 connection 17.5 MB/s; 8 connections 170 MB/s; 32 connections 383 MB/s, with 7 of the 32 requests failing; 64 connections 242 MB/s, with 39 of 64 failing. Time to first byte 0.37 s |
| Aspera from AWS London | Both `fasp.ebi.ac.uk` and `fasp-beta.ebi.ac.uk` reject the public key shipped with the Aspera 3.9.6 client ("failed to authenticate") |
| Opening the zip with `fsspec`'s zip filesystem and `zarr` | Works with no custom code. Python's `zipfile` inflates each member and zarr reads ordinary blosc chunks. |
| Fetching the zip's index (the last 200 MB of the file) | 185 to 216 s with 8 parallel ranged requests; a single request timed out |
| Reading one 1000 × 1000 plane after opening | 28.7 s |
| EBI throughput, one connection | about 136 KB/s on a 20 MB range |
| EBI throughput, 8 parallel connections | 16 MB in 11 s, about 1.5 MB/s; individual connections 180 to 875 KB/s |
| Raw-Deflate codec (earlier prototype) | A codec that calls `zlib.decompress(data, -15)` decodes a real chunk identically to inflate followed by blosc |
| Are the rounds aligned? | Yes. Each round's DAPI against round 1, at pyramid level 3, on wells C/02 and E/05: zero shift, pixel correlation 0.975 to 0.999 |
| Padding | 1,155,683 chunks have the all-zero frame size (80,897 B for images, 161,455 B for labels); one decoded chunk is all zeros |

**Compression on 12 level-0 chunks from well C/02** (240 MB raw):

| Codec | Ratio | Encode speed |
|---|---|---|
| blosc lz4 with shuffle (the source's chunks) | 10.7× | |
| The zip as published (Deflate over blosc-lz4) | 15.6× | |
| **blosc zstd 5, bitshuffle** | **17.7×** | **1,410 MB/s** |
| blosc zstd 9, bitshuffle | 18.6× | 84 MB/s |
| zstd 19 | 17.6× | 17 MB/s |
| pcodec 8 | 16.4× | 474 MB/s |
| pcodec 12 | 16.7× | 440 MB/s |

All 12 chunks come from one well, and 11 of them are at least 64% zeros.
Inside a field of view, where the embryo is, the data compresses much less:

**Codecs on field pixels** (31 planes of one field of view, well C/02,
round 1, every ninth plane; embryo pixels, no filler):

| Codec | DAPI | β-catenin | Encode | Decode |
|---|---|---|---|---|
| blosc lz4, shuffle (the source's) | 2.7× | 2.7× | 1.5–3.6 GB/s | 4.5–4.7 GB/s |
| blosc zstd 3, bitshuffle | 4.4× | 4.5× | 1.6–2.2 GB/s | 3.8–4.7 GB/s |
| **blosc zstd 5, bitshuffle** | **4.4×** | **4.6×** | 0.9–1.2 GB/s | 5.8 GB/s |
| blosc zstd 9, bitshuffle | 4.6× | 4.7× | 17 MB/s | 1.2–1.4 GB/s |
| pcodec 8 | 5.6× | 5.1× | 0.3 GB/s | 1.4 GB/s |
| pcodec 12 | 5.7× | 5.1× | 0.3 GB/s | 1.5–1.6 GB/s |

**Compression of one whole field of view** (well C/02, round 1, all 271
planes, 1000 × 1000 pixels):

| Array | blosc lz4 (source) | blosc zstd 5, bitshuffle |
|---|---|---|
| DAPI | 2.6× | 4.3× |
| β-catenin | 2.7× | 4.6× |
| `nucleiRaw` label (uint32) | 127× | 622× (656× with byte shuffle) |

The zstd ratio is the same, 4.3 to 4.6×, for every chunk shape tried except
single planes of 250 × 250, which drop to 3.8 to 3.9× and encode 10 to 40 times
slower. The store-size estimate follows from these: field data shrinks 1.65×
against the source's lz4, so about 850 GB of non-empty lz4 image frames become
about 515 GB, and the labels about 11 GB.

**Read latency from the managed bucket** (median of 12 ranged GETs):

| Reader | 256 KiB | 4 MiB | 64 requests of 4 MiB at once |
|---|---|---|---|
| AWS us-east-1 | 96 ms | 125 ms | 584 MB/s |
| AWS London | 235 ms | 390 ms | 259 MB/s |
| A laptop in the United States | 132 ms | 382 ms | 57 MB/s |

EU readers pay 250 to 400 ms per request, so the request count of a read
matters more than its bytes. zarr-python keeps 10 requests in flight by
default; from London, 64 in flight doubled throughput.

**Not yet tested:**

- Sustained transfer speed. The London test ran for seconds per setting;
  whether EBI holds 170 MB/s or more for an hour is unmeasured.
- Read times on the real store. The layout comparison is a simulation of
  zarr-python's requests on measured chunk sizes and latencies.
- Conversion throughput on Coiled, and the cost of committing about 5,908
  arrays to Arraylake.
- That every field's level-0 chunks are present. Only one image's chunk
  pattern has been compared against its `FOV_ROI_table`.

## Judgment calls

1. **Stage the zip in R2 first.** Converting straight from EBI would avoid
   storing the zip, but any rerun of the conversion (a bug, a different chunk
   shape or codec) would pull 591 GB from EBI again. R2 charges nothing for
   egress, so reruns from R2 cost only compute. *Decided: stage.*

2. **Transfer method.** EBI offers HTTPS, FTP, Aspera and Globus. HTTPS and
   FTP run over TCP, whose throughput per connection falls with distance: one
   connection gives about 136 KB/s to the United States and 17.5 MB/s to AWS
   London, the region nearest EBI's Hinxton site. Aspera rejected the public
   client key, and Globus needs an interactive login and an endpoint of our
   own, so neither is needed once HTTPS is this fast. *Decided: one VM in
   AWS London fetches 64 MiB ranges over 8 connections and uploads each as one
   part of an S3 multipart upload, with no local disk.* The design follows
   from three requirements:
   - **Resume.** The upload's state lives in the bucket. A rerun finds the
     unfinished upload, lists the parts already stored and sends the rest.
   - **Be kind to EBI.** 8 connections, about a third of the point where EBI
     starts refusing requests and enough for about 170 MB/s. Failures back off
     exponentially with random jitter up to 5 minutes and honour
     `Retry-After`; five consecutive failures pause every request for 5
     minutes. Requests carry a User-Agent naming the copy and Earthmover.
   - **Check every byte.** R2 rejects a part whose `Content-MD5` does not
     match, and each part's ETag must equal the MD5 that was sent.
   Progress prints every minute, and a `--status` mode reads the stored parts
   from the bucket from anywhere.

3. **Plate layout.** The source stores each round as a
   well-sized image holding the well's 5 to 9 fields of view at their stage
   positions; everything between them was never imaged and is exactly 0. The
   chunk grid starts at the well image's corner, so each field straddles two to four
   grid cells. *Decided: one image per embryo, the structure OME-Zarr's HCS
   layout provides for fields, holding every round's channels.* An image allows
   only one axis that is neither space nor time, so rounds cannot be an axis
   of their own; the published rounds are already registered to each other, so
   they share one image as 16 channels, in round order. Every chunk is one plane
   of one channel, so the same chunks could also serve one image per round, if
   Icechunk gains a way to share chunk references between arrays
   (icechunk#2507). Each embryo becomes exactly one
   grid cell, so every embryo read needs a third of the requests: an embryo
   volume from London drops from about 2.0 s to 1.3 s (in 32-plane shards),
   all 16 channels from 29 s to 20 s; the bytes fetched are the same. The cost: Fractal's tools expect one well image per
   round and will not work on the store as written; the labels must be cut
   per field and shifted to each field's origin, and the cut verified against
   the well image; and laying a whole well out depends on the
   viewer applying each image's translation, which is untested. The
   rejected alternative keeps the source's well image, with the same chunk
   and shard shapes.

4. **Round 3.** Every well holds four images, but the plate lists
   acquisitions 0 to 2 and each well lists images 0 to 2. A reader that
   follows the HCS metadata never finds round 3 (Nanog, YAP, XRN2).
   *Decided: every image holds all four rounds' channels, and the plate's
   attributes record each round's channels; record the omission in
   `LAYOUTS.md` for the authors.*

5. **Chunks and shards.** Object stores bill per object written: R2 charges
   $4.50 per million writes, so one-plane chunks without sharding would write
   about 5.0 million objects, about $23, and would leave millions of small objects to list and garbage-collect. Sharding packs many chunks
   into one object with an index at its end, so a reader still fetches one
   small chunk by byte range. The deciding criterion is how reads feel to the
   tutorial's users, in molab next to the data and on laptops in Europe; write
   cost is under $4 for any sharded layout. *Decided: for images,
   `1 × 1 × 1000 × 1000` chunks, one whole plane, in shards of
   `1 × 32 × 1000 × 1000`; for labels, `1 × 1000 × 1000` chunks in shards that
   span all 271 planes.* Single-plane chunks keep viewing a plane to two small requests;
   32-plane shards make an embryo volume 9 to 10 requests of about 15 MB that
   download in parallel. Shallower 10-plane shards need three times the
   requests; full-depth shards would make a volume one 128 MB request on one
   connection, about 5 s from London; 3D blocks of `8 × 250 × 250` match
   32-plane shards for volumes and fetch seven times the bytes to show a plane.
   With one image per field the store writes about 126,600 objects. Step 3
   builds one well in this layout and again in 10-plane shards, and times real reads from in-region and from London before the
   full run. The tutorial should set
   `zarr.config.set({"async.concurrency": 64})`, which helps EU readers more
   than any layout does.

6. **Codec.** *Decided: blosc zstd 5 with bitshuffle.* On field pixels
   pcodec 8 compresses better (5.6× against 4.4× for DAPI, 5.1× against 4.6×
   for β-catenin) but decodes at 1.4 GB/s against 5.8 GB/s, and on fast
   connections decode speed matters more than the bytes saved. blosc is also
   one of the codecs the Zarr v3 core specification defines, so every Zarr
   implementation reads it, including browser viewers; pcodec is not. Higher
   zstd levels gain little: level 9 reaches 4.6× and 4.7× and encodes at
   17 MB/s. Measure the label arrays before
   the full run; they may compress better with a different setting.

7. **ROI tables.** The source's three tables describe the layout rather than
   the biology: where fields sit on the well image, the well image's extent,
   and one bounding box per well around a mask in which every embryo has the
   same label value. Once each field is its own image positioned by a 0.6
   scene, each table is either structure or derivable. *Decided: fold
   them into the layout and keep their values as attributes*, so no Iceberg
   tables are written. The repository's rule for tabular data does not apply,
   because no table remains.

8. **A virtual store over the staged zip.** With the zip in R2 and the
   raw-Deflate codec, a virtual Icechunk store could reference it within a day,
   before the conversion. It would need the custom codec to read, and it would
   be replaced by the native store. *Decided: not now; explore the staged
   zip directly with `fsspec` and `zarr`, which works today.*

9. **The staged zip after conversion.** *Decided: keep it until the
    native store passes verification, then delete it.* EBI remains the
    archive of record.

10. **Destination.** The store goes to `earthmover-biology/zebrafish-3d-4i`
    on the org's Arraylake managed storage, a Cloudflare R2 bucket. The zip is
    staged in the same bucket at
    `files/ingest-staging/bioimage-archive/S-BIAD3228/AssayPlate_Greiner_655090.zarr.zip`,
    which Arraylake addresses as
    `arraylake://earthmover-biology/bucket/earthmover-managed/files/ingest-staging/...`.
    Repos live under `{id}_{name}/` prefixes, so `files/` cannot collide with
    one, and Icechunk's garbage collection never leaves a repo's prefix.

11. **OME-Zarr 0.6.** This ingest writes 0.6 (decided 2026-10-27), because cutting each well image
    into per-field images needs a way to keep each field's position, and 0.6's
    coordinate transformations provide one. Each field image keeps its own
    `intrinsic` coordinate system; a `scene` above a well's images declares a
    `well` coordinate system, listed first so viewers display it by default,
    and maps each image into it with a translation: the field's position on the
    source well image, from its `FOV_ROI_table` row. All four rounds of a field
    get the same translation, which records that the published rounds are
    aligned. Open before the trial: whether one group may carry both the HCS
    `well` metadata and a `scene`, which viewers display a scene, and why each
    field's `x_micrometer_original` is about half its `x_micrometer`.
    `minus80.ome` already builds and validates 0.6 image and label metadata.

12. **Pyramid levels 1 to 3.** The source computed its lower levels on the
    whole well image, and most field origins do not fall on a whole pixel
    there: of 211 fields, 129 (x) and 120 (y) do at level 1, 57 and 48 at
    level 3. A field cut from a lower level therefore cannot be exactly half,
    a quarter or an eighth of 1000 pixels. *Decided: copy the source's
    pixels unchanged for the first commit*, so every value in the store is
    the authors'. Each field's level-k array spans the source pixels its
    level-0 field touches, `floor(origin / 2^k)` to `ceil((origin + 1000) / 2^k)`:
    500 or 501 pixels at level 1, 250 or 251 at level 2, 125 or 126 at level 3,
    and no window runs past its well image's edge. Each level's
    multiscales entry adds a translation of up to one coarse pixel, so it lines
    up with level 0. The edge pixels average the field with the zero filler
    around it, as they do in the source. A later commit rebuilds levels 1 to 3
    from each field's level 0, with exact shapes and no fringe; the first
    commit stays in the history.

## Steps, scope and risk

| Step | What it does | Writes | Approval |
|---|---|---|---|
| 1 | Speed test from AWS London: parallel HTTPS and Aspera | Nothing kept | **Done** 2026-10-27 |
| 2 | Write and delete a one-byte test object to confirm the token can write outside a repo; then transfer the zip, checking each part's MD5, and the final size against EBI's | Managed bucket: one 591 GB object | Yes |
| 3 | Convert one well three times, reading straight from EBI: `main` (16 channels per embryo, 32-plane shards), `per-round` (one image per embryo and round), `shards-10` (10-plane shards); time the six reads from in-region and from London; open all three in napari, with a domain expert | Arraylake branches | Yes (repo creation) |
| 4 | Full conversion on Coiled, reading R2 | Arraylake `main` | Yes |
| 5 | Verify: metadata gates on every array; pixels against the zip on a sample of chunks from every well, round and level, levels 1 to 3 compared exactly | Nothing | |
| 6 | Write `REPORT.md` and `LAYOUTS.md`; delete the staged zip | R2 delete | Yes |

**Costs** (R2 prices from [Cloudflare](https://developers.cloudflare.com/r2/pricing/)):

| Item | Estimate |
|---|---|
| Speed test | under $1 (2 minutes of an m6i.2xlarge at $0.38 an hour) |
| Transfer VM | about $1 (an hour or two of the same VM) |
| AWS egress, transfer: the VM uploads the 591 GB zip to R2, outside AWS | about $53 (at about $0.09/GB) |
| AWS egress, conversion: Coiled workers write the 520 GB store to R2 | about $47; reading the zip from R2 into AWS is free |
| R2 writes: one zip upload, about 126,600 shards | under $1 |
| R2 storage while both exist (591 GB zip plus about 520 GB store) | about $17 a month |
| R2 storage after the zip is deleted | about $8 a month |
| Coiled conversion | unmeasured; priced after step 3 |

**Scope.** In: all 30 wells, four rounds, four levels, the six segmentations
and the values of the three ROI tables, as structure and attributes. Out: the per-object feature tables the preprint
describes, which are not in this deposit, and any re-registration of the
rounds.

**Risks.**

- **EBI throughput.** The London rates come from seconds-long bursts. EBI may
  throttle a sustained transfer. The transfer backs off, logs its rate every
  minute, and resumes from the parts already stored if it stops.
- **An abandoned upload.** Parts of an unfinished multipart upload are billed
  as storage until the upload completes or is aborted. If the transfer is
  given up, abort the upload.
- **The 520 GB estimate** comes from one field of view's compression applied
  to the zip's lz4 frames. Step 3 measures a whole well.
- **Commit cost.** One image per embryo means about 5,908 arrays, each with
  its own manifest: 844 image arrays (211 images × 4 levels) and 5,064 label
  arrays (211 fields × 6 labels × 4 levels). The well-image layout would have
  1,200. Time a commit against Arraylake in step 3.
