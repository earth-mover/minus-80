# Exploring the S-BIAD3228 zip

The source of this ingest is one 591 GB zip of an OME-Zarr plate, with two
million members. Converting it efficiently depends on knowing exactly how it
is laid out: where each well's bytes are, in what order the arrays and planes
come, where the metadata sits, and which members are damaged. `PLAN.md`
decision 14 and `LAYOUTS.md` state those facts. The tools in this folder let
you check every one of them against the real file, from the zip format down
to a single chunk and back up to the layout of the whole file.

The guide runs in seven steps:

1. the end of the file → 2. the table of contents → 3. one member in the
file → 4. inside one member → 5. the Zarr metadata → 6. the layout of the
whole zip → 7. claims to check.

One chunk is the running example for steps 2 to 4: `C/02/2/0/1/13/0/7`.
Its name says well C/02, round 2, pyramid level 0, channel 1, z index 13
(planes 130 to 139), grid row 0, grid column 7.

## The tools

| File | What it does |
|---|---|
| `inputs.py` | Loads the zip's central directory (fetched once, kept as Parquet) and a well's metadata (fetched once, kept as JSON) |
| `layout.py` | Computes the layout from those inputs, as pure functions over data frames; its docstring defines the terms used here |
| `zipmap.py` | The command line: overview, runs, order, member, field, ranges, meta, and `html` for the interactive map (`map.html` and `map.js`) |
| `peek.py` | Fetches and decodes one raw zip structure at a time, field by field, for steps 1 to 4 |
| `central_directory.py` | A marimo notebook that shows the whole central directory as a table you can filter |

The library code they build on is `minus80.parsers.zip`: `directory`
parses a central directory and members from bytes, and `reading` fetches the
bytes through `minus80.core.fetch` and parses them.

Run everything from the repository root, in the ingest's `explore`
dependency group. Two shell functions save typing:

```sh
Z() { uv run -q --package zebrafish-3d-4i-ingest --group explore python ingests/zebrafish-3d-4i/explore/zipmap.py "$@"; }
P() { uv run -q --package zebrafish-3d-4i-ingest --group explore python ingests/zebrafish-3d-4i/explore/peek.py "$@"; }
```

**The first run fetches the central directory**, the last 178.7 MB of the
zip, and keeps it as Parquet in the cache directory (`$MINUS80_CACHE_DIR`,
else `~/.cache/minus80`). From a laptop in the United States that took
10 minutes; from AWS London, seconds. After that, commands that need only
the directory run offline in about 2 s. A command that needs a well's
metadata fetches about 50 small members the first time, about 10 s from the
United States, and caches them too. `peek.py` fetches a few bytes on every
run.

EBI's server is slow to accept connections from far away, so the fetches use
`minus80.core.fetch`'s `patient=True` settings. A command that fails with a
connection timeout usually works on a second try.

---

## 1. Where does a zip keep its table of contents?

A zip is a sequence of members followed by a table of contents. Each member
is a 30-byte *local header*, the member's name, an *extra field*, and the
compressed data. A writer that streams its output does not know a member's
size until it has written the data, so it leaves the sizes in the local header
as zeros, sets flag bit 3, and writes a 16-byte *data descriptor* (CRC and
sizes) after the data. The table of contents is the *central directory*. It
repeats every member's name and sizes and records where its local header
starts. The file ends with short records that say where the central
directory is:

```
offset 0                                                              end of file
│ local header │ name │ extra │ data │ descriptor │ local header │ ... │
│ central directory: one entry per member (name, sizes, local-header offset)  │
│ ZIP64 end record │ ZIP64 locator │ end of central directory record      │
```

The classic end record has 16- and 32-bit fields. A zip larger than 4 GiB,
or with more than 65,535 members, sets those fields to all ones and keeps
the real values in the *ZIP64 end record*. The *ZIP64 locator*, just before
the end record, says where that record is. Each record starts with a
four-byte signature: `PK\x05\x06` for the end record, `PK\x06\x07` for the
locator and `PK\x06\x06` for the ZIP64 end record.

`peek.py end` fetches the last 120 bytes and decodes all three:

```sh
P end
```

```
end of central directory record at byte 591,288,782,526
  entries 0xffff, directory size 178,730,778, directory offset 0xffffffff
  (0xffff and 0xffffffff mean: too large for this field, see the ZIP64 record)
ZIP64 locator at byte 591,288,782,506: the ZIP64 end record is at byte 591,288,782,450
ZIP64 end record at byte 591,288,782,450
  entries 2,036,714
  central directory: 178,730,778 bytes, from byte 591,110,051,672 to 591,288,782,450
```

The zip holds 2,036,714 members. Its central directory is 178.7 MB and ends
exactly where the ZIP64 record begins.

**Takeaway:** a reader has to fetch the last 179 MB of the file before it can
find any member. One request for all of it ran for minutes with nothing to
show from AWS London; `minus80.parsers.zip.reading` fetches it in 8 MiB
pieces, 8 at once.

## 2. What does the table of contents say about one member?

Each central-directory entry starts with `PK\x01\x02`. `peek.py entry`
fetches one entry from the file and decodes it. Compare the running example,
below 4 GiB, with a member past it:

```sh
P entry C/02/2/0/1/13/0/7
P entry E/02/.zattrs
```

```
directory entry at byte 591,110,395,591, 72 bytes: signature b'PK\x01\x02'
  name 'C/02/2/0/1/13/0/7'
  version made by 20 (system 0, zip 20), needed 20
  flags 0x0808: data descriptor True, UTF-8 name True
  method 8 (8 is Deflate), DOS date 33 (33 is 1980-01-01), CRC 9b0584fc
  compressed 4,231,287, uncompressed 6,189,086
  local header offset field 0xed60ef84
  extra field, 9 bytes: 0x5455 (5 bytes: 0100000000)
  so the local header starts at byte 3,982,552,964

directory entry at byte 591,137,820,759, 79 bytes: signature b'PK\x01\x02'
  name 'E/02/.zattrs'
  version made by 45 (system 0, zip 45), needed 45
  flags 0x0808: data descriptor True, UTF-8 name True
  method 8 (8 is Deflate), DOS date 33 (33 is 1980-01-01), CRC 079f65cd
  compressed 103, uncompressed 351
  local header offset field 0xffffffff (see ZIP64 block)
  extra field, 21 bytes: 0x0001 (8 bytes: 7857022f16000000), 0x5455 (5 bytes: 0100000000)
  so the local header starts at byte 95,277,963,128
```

Five facts come out of these two entries, and the notebook shows that each
holds for every entry:

- **Every member is Deflate-compressed** (method 8).
- **Every member has a data descriptor.** The flags are `0x0808`: bit 3 for
  the descriptor, bit 11 for a UTF-8 name.
- **Timestamps carry no information.** The DOS date is 1980-01-01 and the
  `0x5455` block holds a Unix time of 0.
- **Past 4 GiB the offset moves into the extra field.** For `E/02/.zattrs`
  at 95.3 GB the 32-bit offset field is `0xffffffff`, and a ZIP64 block
  (`0x0001`) holds the real 8-byte offset, so the extra field grows from 9 to
  21 bytes and the version needed rises from 20 to 45.
- **The writer stored no host system** (system 0, MS-DOS) and set UTF-8 on
  ASCII names. Both match Java's `java.util.zip.ZipOutputStream`, which also
  always writes data descriptors; who packed the zip is not documented.

To browse every entry, open the notebook:

```sh
uv run --package zebrafish-3d-4i-ingest --group explore marimo edit ingests/zebrafish-3d-4i/explore/central_directory.py
```

Its last cell counts each field's values across all entries: 4,947 entries
made by version 20 and 2,031,767 by version 45, and one value for the flags
and the method.

**Takeaway:** the directory says where each member's local header starts,
not where its data starts. That depends on the local header's own name and
extra-field lengths, which step 3 reads.

## 3. What does one member look like in the file?

`peek.py local` fetches the member's local header and the bytes just after its
data:

```sh
P local C/02/2/0/1/13/0/7
P local E/02/.zattrs
```

```
local header at byte 3,982,552,964: signature b'PK\x03\x04', flags 0x0808, method 8
  CRC 0x0, compressed 0, uncompressed 0: zeros, the sizes follow the data
  name 'C/02/2/0/1/13/0/7', extra field 9 bytes
  data: bytes 3,982,553,020 to 3,986,784,307 (4,231,287 bytes, from the directory)
data descriptor at byte 3,986,784,307: signature b'PK\x07\x08', CRC 9b0584fc, compressed 4,231,287, uncompressed 6,189,086
next local header at byte 3,986,784,323: starts with b'PK\x03\x04'

local header at byte 95,277,963,128: signature b'PK\x03\x04', flags 0x0808, method 8
  CRC 0x0, compressed 0, uncompressed 0: zeros, the sizes follow the data
  name 'E/02/.zattrs', extra field 9 bytes
  data: bytes 95,277,963,179 to 95,277,963,282 (103 bytes, from the directory)
data descriptor at byte 95,277,963,282: signature b'PK\x07\x08', CRC 079f65cd, compressed 103, uncompressed 351
next local header at byte 95,277,963,298: starts with b'PK\x03\x04'
```

The local header's CRC and sizes are zero, because the writer streamed the
data and wrote them afterwards, in the descriptor. The data starts 56 bytes
after the header (30 bytes of header, 17 of name, 9 of extra field), the
descriptor follows the data, and the next member's local header begins
right after the descriptor. `E/02/.zattrs` has a 9-byte extra field in its
local header but a 21-byte one in its directory entry, so the directory's
lengths cannot place its data.

**Takeaway:** the members sit back to back, local header to local header.
So a copy fetches each member from its local header to the next member's
local header, which holds everything, and reads the local header from those
bytes. That is what `minus80.parsers.zip.directory.parse_member` does. A
*virtual* reference has to point at the exact first byte of the data, which
the directory alone cannot give here; VirtualiZarr's zip parser then reads
every local header, two million requests. This ingest copies, so it never
needs that.

## 4. What is inside one member?

A chunk member is a Blosc frame, compressed a second time by the zip with
raw Deflate. `peek.py chunk` fetches the member, inflates it, checks the
CRC against the directory, reads the 16-byte Blosc header, and decodes the
frame with the codec its array's `.zarray` names:

```sh
P chunk C/02/2/0/1/13/0/7
```

```
C/02/2/0/1/13/0/7: inflated to 6,189,086 bytes, CRC checked against the directory
Blosc header: version 2, flags 0x21 (byte shuffle: True), type size 2
  decoded size 20,000,000, block size 262,144, frame size 6,189,086
decoded with {'blocksize': 0, 'clevel': 5, 'cname': 'lz4', 'id': 'blosc', 'shuffle': 1}: shape (1, 10, 1000, 1000), dtype uint16
  min 0, max 1262, mean 24.0, zeros 36.0%
```

| Bytes of the frame | Field | Here |
|---|---|---|
| 0 | format version | 2 |
| 2 | flags: bit 0 byte shuffle, bits 5 to 7 the compressor (1 is LZ4) | `0x21` |
| 3 | type size | 2 (uint16) |
| 4–8 | decoded size | 20,000,000 = 10 × 1000 × 1000 × 2 |
| 8–12 | block size | 262,144 |
| 12–16 | frame size, header included | 6,189,086 |

The frame size in bytes 12 to 16 lets you check that a frame is whole without
decoding it. The zip's Deflate pass shrank this frame from 6.19 MB to
4.23 MB.

Four other members show what else the zip holds:

```sh
P chunk C/02/2/2/2/19/3/2                       # padding
P chunk C/02/1/labels/Pol-II-S2P_foci/1/6/0/3   # cut short
P chunk C/02/1/labels/cells/0/26/0/7            # empty
P chunk C/02/3/3/2/20/1/1                       # padding-sized, but not padding
```

```
C/02/2/2/2/19/3/2: inflated to 80,897 bytes, CRC checked against the directory
  decoded size 20,000,000, block size 262,144, frame size 80,897
  min 0, max 0, mean 0.0, zeros 100.0%

C/02/1/labels/Pol-II-S2P_foci/1/6/0/3: inflated to 16,384 bytes, CRC checked against the directory
Blosc header: version 2, flags 0x21 (byte shuffle: True), type size 4
  decoded size 40,000,000, block size 524,288, frame size 162,569
cut short: the header promises 162,569 bytes but only 16,384 are present

C/02/1/labels/cells/0/26/0/7: inflated to 0 bytes, CRC checked against the directory
an empty member: there is no Blosc frame to decode; it reads as 0

C/02/3/3/2/20/1/1: inflated to 161,455 bytes, CRC checked against the directory
  decoded size 20,000,000, block size 262,144, frame size 161,455
  min 0, max 110, mean 0.1, zeros 99.1%
```

- **Padding**: an all-zero image chunk is always an 80,897-byte frame, and
  an all-zero label chunk 161,455 bytes.
- **Cut short**: the zip holds exactly 16 KiB of a 162,569-byte frame, and
  the CRC matches, so the bytes were already short when they were zipped.
- **Empty**: a zero-length file, which no reader can decode.
- **Padding-sized but real**: this image chunk has a label's padding size
  and holds real pixels, so the size marks padding only together with the
  member's kind.

**Takeaway:** every chunk costs two decodes, an inflate and then a Blosc
decode. An empty member shows in the directory as an uncompressed size of 0;
a cut-short one shows only once inflated, when its header's frame size
exceeds the bytes present.

## 5. What does the Zarr metadata say?

Zarr v2 keeps each group's and array's metadata in small JSON members:
`.zarray` (shape, chunk shape, data type, compressor), `.zattrs` (attributes)
and `.zgroup` (marks a group). `zipmap.py meta` fetches and prints one:

```sh
Z meta C/02/2/0/.zarray
```

```
{"chunks": [1, 10, 1000, 1000], "compressor": {"blocksize": 0, "clevel": 5, "cname": "lz4", "id": "blosc", "shuffle": 1}, "dimension_separator": "/", "dtype": "<u2", "fill_value": 0, "filters": null, "order": "C", "shape": [4, 271, 12425, 13187], "zarr_format": 2}
```

The label arrays differ only in having no channel axis and 32-bit values:
`Z meta C/02/1/labels/nucleiRaw/0/.zarray` gives chunks `[10, 1000, 1000]`,
dtype `<u4` and shape `[271, 12425, 13187]`. Each pyramid level halves y and
x, rounding down: `C/02/2/1` to `C/02/2/3` have shapes `[4, 271, 6212, 6593]`,
`[4, 271, 3106, 3296]` and `[4, 271, 1553, 1648]`.

The plate's `.zattrs` (`Z meta .zattrs`) lists rows B to G, 30 wells and
three acquisitions, numbered 0 to 2. One well and the labels group:

```sh
Z meta C/02/.zattrs
Z meta C/02/1/labels/.zattrs
```

```
{"well": {"images": [{"acquisition": 0, "path": "0"}, {"acquisition": 1, "path": "1"}, {"acquisition": 2, "path": "2"}], "version": "0.4"}}
{"labels": ["cells", "cyto", "embryoRaw", "Pol-II-S2P_foci", "membrane", "nucleiRaw"]}
```

(`meta` prints the JSON indented; it is shown on one line here.)

Every well holds rounds 0 to 3, but the plate and the well list only 0 to 2,
so a reader that follows the HCS metadata never finds round 3. Round 3 also
differs between wells: `Z meta C/06/3/.zattrs` names its channels DAPI_3,
H3K27Ac_restained, bCatenin_restained and pH3_restained, where most wells
have DAPI_3, Nanog, YAP and XRN2 (`LAYOUTS.md` lists the nine wells).

Field positions live in each round's `FOV_ROI_table`, an AnnData group of
27 members. `inputs.load_well_metadata` fetches those members and reads the
table with anndata, so the tools name fields `FOV_1` to `FOV_9`.

**Where the documents sit.** Each document is its own member, stored beside
its array's chunks, so the documents are spread across the whole 591 GB:
`Z runs C/02` shows them as `[meta]` and `[table]` rows between the arrays,
and the plate's own `.zattrs` sits at 95.3 GB, between wells C/07 and E/02.
Opening the zip with zarr therefore costs one request per document as zarr
walks the hierarchy; an earlier version of this ingest spent 160 to 246 s on
that from EBI. The tools fetch only the few documents they need, by name,
since the directory already says where each one is.

## 6. How is the whole zip laid out?

### 6.1 In what order are the wells?

```sh
Z overview
```

```
2,036,714 members; the last one ends at 591.11 GB

│ kind  ┆ members ┆ zip bytes    ┆ empty ┆ padding │
│ image ┆ 1474368 ┆ 570142753500 ┆ 618   ┆ 792692  │
│ label ┆ 552888  ┆ 20812109252  ┆ 626   ┆ 362991  │
│ table ┆ 7530    ┆ 476267       ┆ 0     ┆ 0       │
│ meta  ┆ 1928    ┆ 363079       ┆ 0     ┆ 0       │

wells in file order:
  C/02      0.0 –    20.3 GB   20.31 GB   57,603 members   23 empty
  C/05     20.3 –    38.6 GB   18.27 GB   59,451 members   18 empty
  ...
  B/07    572.5 –   591.1 GB   18.59 GB   88,403 members   24 empty

plate-level members, outside every well:
  C/.zgroup      at    59.1 GB
  .zattrs        at    95.3 GB
  ...
```

Each well is one contiguous region of 14 to 27 GB. The wells come in row
order C, E, G, F, D, B, and within row C the columns come 02, 05, 04, 06, 07.

### 6.2 What is inside one well, in order?

```sh
Z runs C/02
```

```
C/02: 57,603 members, bytes 0.00 – 20.31 GB

  #     start       size  members empty   pad  what
  1    0.000G     0.2 kB        1     0     0  [meta] C/02
  2    0.000G     0.5 kB        1     0     0  [meta] C/02/2
  3    0.000G    0.26 GB    1,793     0   896  C/02/2/2
  4    0.259G    66.2 MB      449     0     0  C/02/2/3
  5    0.325G     4.5 kB       27     0     0  [table] C/02/2/tables/FOV_ROI_table
  6    0.325G     4.2 kB       27     0     0  [table] C/02/2/tables/well_ROI_table
  7    0.325G     0.2 kB        2     0     0  [table] C/02/2/tables
  8    0.325G     0.1 kB        1     0     0  [meta] C/02/2
  9    0.325G    4.04 GB    2,689     0     1  C/02/2/0
 10    4.367G    1.03 GB    5,489     0  4368  C/02/2/1
 ...
```

Each array's chunks form one contiguous *run*. The rounds come 2, 3, 0, 1;
within a round the levels come 2, 3, 0, 1, with the round's tables between
level 3 and level 0; round 1's six labels come before round 1's image. A
run's member count includes the array's own `.zarray`, which sits partway
through the run (2.23 GB into `C/02/2/0`), so the counts end in 1 or 9.

**Why are the runs such different sizes?** A run's size follows from how
many grid cells its level stores. Every run has 112 (channel, z index)
groups: 4 channels × 28 z indices.

| Level | Shape (y, x) | Grid cells | Cells stored | Chunks | Padding | Size |
|---|---|---|---|---|---|---|
| 0 | 12,425 × 13,187 | 13 × 14 = 182 | 24, only cells some field overlaps | 2,688 | 1 | 4.04 GB |
| 1 | 6,212 × 6,593 | 7 × 7 = 49 | all 49 | 5,488 | 4,368 | 1.03 GB |
| 2 | 3,106 × 3,296 | 4 × 4 = 16 | all 16 | 1,792 | 896 | 0.26 GB |
| 3 | 1,553 × 1,648 | 2 × 2 = 4 | all 4 | 448 | 0 | 66 MB |

Level 0 stores only the cells some field overlaps, but each of those is full
of image data, so it is large. Levels 1 to 3 store every cell, including the
space between fields, so they have many members; but a padding member
deflates to about 650 bytes, so they stay small.

### 6.3 What order are the chunks in, inside one array?

```sh
Z order C/02/2/0
```

```
C/02/2/0: 2,688 chunks in 112 (channel, z index) groups, 4.04 GB

   #        at  c   z chunks      size
   1        0M  2  19     24   29.3 MB
   2       29M  2  17     24   31.1 MB
   3       60M  2  25     24   26.7 MB
   4       87M  2   6     24   34.3 MB
   5      121M  2  18     24   30.1 MB
  ...
```

Each channel's chunks are contiguous, and within a channel each z index's
24 cells are contiguous: 112 groups, the fewest possible. The channels come
2, 3, 0, 1, the same order as the rounds. The z indices come 19, 17, 25, 6,
18, 24, …, in an order that is neither numeric nor alphabetical, and the same
in every channel and in all 120 level-0 runs (step 7). So a 32-plane output
shard, which needs four consecutive z indices, can only be written once
most of its channel has arrived.

### 6.4 What is one chunk, and which field needs it?

```sh
Z member C/02/2/0/1/13/0/7
```

```
         name: C/02/2/0/1/13/0/7
         kind: image
        start: 3,982,552,964
          end: 3,986,784,323
   compressed: 4,231,287
 uncompressed: 6,189,086
        crc32: 9b0584fc
        flags: 0x0808
         ...
  within the well: 3.983 GB in, in 64 MiB range #59
  planes 130–139; fields overlapping this grid cell: FOV_1
```

### 6.5 What does one field need from the zip?

```sh
Z field C/02 FOV_1
```

```
C/02 FOV_1: x 4326.400000000001 µm, y 0.0 µm
level 0: rows (0, 1000), columns (6656, 7656) → grid cells [(0, 6), (0, 7)]
  C/02/2/0                           224 chunks   0.58 GB   112 separate byte spans: one request each if read alone
  ...
  C/02/1/labels/nucleiRaw/0           56 chunks    2.8 MB    28 separate byte spans: one request each if read alone
```

The field overlaps two grid cells. In each round's level 0 it needs those two
cells in each of the 112 groups, and the groups are 27 to 40 MB apart, so
reading one field on its own costs 112 requests per round.

### 6.6 How many requests does reading a whole well take?

```sh
Z ranges C/02
```

```
C/02: a region of 20.31 GB
  the whole region in 64 MiB ranges: 303 requests
  only the chunks to decode: 26,675 chunks, 20.28 GB
    merged across gaps up to 1 MiB: 1 spans, 20.31 GB, 303 requests of up to 64 MiB
    R2 in-region     5 waves × 100 ms =   0.5 s of waiting;  34.8 s of transfer at 584 MB/s
    EBI from London  19 waves × 370 ms =   7.0 s of waiting;  72.5 s of transfer at 280 MB/s
```

The chunks a conversion must decode fill the well's region almost entirely,
so skipping the rest saves 0.03 GB. Larger ranges cut the request count
(`--mib 256` gives 76) but save less than a second of waiting; the transfer
takes as long.

### 6.7 The interactive map

```sh
Z html --wells C/02,E/04
```

This writes `zip-map.html` to the cache directory: one self-contained page
(about 3 MB per well) to open in a browser. It shows the whole zip, the
central directory split by well, a decoder for member names, one well's runs
and chunks (zoomable, colored by round, level, channel, z index or status),
the well's chunk grid with its fields, and the runs as a table. Hovering any
part explains it.

## 7. Claims to check

Each of these comes from `PLAN.md` or `LAYOUTS.md`.

1. **The zip has 1,244 empty members.** `Z overview` gives 618 empty image
   members and 626 empty label members. Step 4 decodes one.
2. **Padding is recognizable by size and kind.** In Python, from the
   `explore` folder:
   ```python
   import polars as pl, layout
   from inputs import load_central_directory, load_directory_facts
   members = layout.describe_members(load_central_directory(), load_directory_facts()["directory_offset"])
   chunks = layout.chunks_only(members)
   chunks.filter(pl.col("uncompressed").is_in([80_897, 161_455])).group_by("kind", "uncompressed").len()
   ```
   gives 792,692 image chunks at 80,897 bytes, 362,991 label chunks at
   161,455, and 4 image chunks at 161,455, one of which step 4 shows to hold
   real pixels.
3. **Every well's rounds come in the order 2, 3, 0, 1.** Continuing:
   ```python
   first = chunks.group_by("well", "round").agg(pl.min("start")).sort("start")
   {tuple(rounds) for rounds in first.group_by("well", maintain_order=True).agg(pl.col("round"))["round"]}
   ```
   gives `{(2, 3, 0, 1)}`.
4. **The z order is the same in every level-0 run.** Continuing:
   ```python
   level0 = chunks.filter((pl.col("kind") == "image") & (pl.col("level") == 0))
   orders = {tuple(zip(*layout.chunk_order(run.sort("start")).select("c", "z")))
             for _, run in level0.group_by("array")}
   len(orders)
   ```
   gives 1, over 120 runs.
5. **The plate's `.zattrs` sits in the middle of the file, at 95.3 GB.**
   `Z overview`, last block.
6. **A cut-short frame shows in its header.**
   `P chunk C/02/1/labels/Pol-II-S2P_foci/1/6/0/3` (step 4).
