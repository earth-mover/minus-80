# Formats

This page covers which sources can be virtualized, how to turn a file into
chunk references, and the library traps on the way. How to shape and verify a
whole store is in [ingest.md](ingest.md).

A virtual chunk is a byte range in a source file plus the codec that decodes
it. A source can be virtualized when each of its storage units decodes starting
from its own offset, and the units land on a grid that an array reader can
index by position.

## What cannot be virtualized

### Transcode a stream that cannot be decoded from an offset

Whole-file gzip or DEFLATE (`.nii.gz`, `.fastq.gz`) cannot be virtualized,
because DEFLATE back-references run from the start of the stream. Transcode it
once, to uncompressed data or to a blocked compression such as BGZF.
Compression alone is not the problem: an LZW TIFF with one strip per file can
be virtualized, because the strip is the unit.

### Convert records that carry their own coordinate

If each record stores its own position, the chunk index means nothing. bigWig
records hold `(start, end, value)`, which no array engine can select on, so
convert them to a position-indexed array. Ragged record streams (BAM, CRAM,
VCF) are tables: convert them with vcf2zarr or serve them with htsget.

### Write variable-length strings natively

HDF5 keeps variable-length strings and categoricals on a heap, not in
fixed-stride buffers. Reference the numeric datasets and write the strings
natively. VirtualiZarr's `HDFParser` raises on heap-backed datasets instead of
skipping them, so classify the datasets with h5py first and pass every
non-numeric one in `drop_variables`.

### Convert files that are too slow to range-read

SQLite fetches one page per request, so reading one site's nuclei from a
23.5 GB CellProfiler database takes 112 s. Convert the part someone needs, for
example to Parquet with CytoTable.

## Turning a file into chunks

### Let the format's library locate the bytes

Never compute an offset by arithmetic: assuming DICOM PixelData ends the file
breaks on files with trailing elements. Read endianness and the data offset
from the header with the format's library, and pass endianness to the Zarr
`bytes` codec.

### Write a padded final chunk natively

HDF5 and some tiled formats pad an array's last chunk to full size. When arrays
are concatenated across files, that padding corrupts the next file's data.
Write each file's last chunk natively, holding only its valid elements.

### Give each array its own dimensions

VirtualiZarr's `to_icechunk` goes through an xarray `Dataset`, which names axes
`phony_dim_0` and refuses arrays that share a dimension name with different
lengths, as sparse matrices and pyramid levels do. Write with
`ManifestGroup.to_icechunk` (VirtualiZarr#1108), which does not build a
`Dataset`, and pass `mode="a"` to write into an existing group.

### Write an NWB file whole, in the unified Zarr v3 convention

hdmf-zarr 0.14 reads NWB from Zarr v3, under a convention it shares with
LINDI and shadowzarr: links in a `_LINKS` group attribute, dataset types in
`_DTYPE`, scalars as shape-() arrays and references as
`{"_REFERENCE": {"source", "path"}}`
([storage.rst](https://github.com/hdmf-dev/hdmf-zarr/blob/0.14.0/docs/source/storage.rst)).
Write an NWB file's whole tree in it, rather than its numeric arrays alone,
and the group keeps NWB's paths, types and references: a store holding one
file at its root opens with `NWBZarrIO` as a typed `NWBFile`, with the
electrodes table and every other reference resolved. shadowzarr's
`generate_rfs` maps the file and `rfs_to_icechunk` writes it;
`minus80.parsers.nwb.unified` places it at a group of a larger store and adds
`dimension_names`, which the convention leaves out and xarray needs.
`NWBZarrIO` ignores the `dimension_names`, and it ignores extra attributes,
such as a CF `scale_factor`, as long as NWB's own are left in place.

`NWBZarrIO` opens only a store's root, so it cannot open a file kept at a
group of a larger store; zarr and xarray read it, and pynwb waits for
hdmf-zarr to accept a group. Keep one repo per dataset anyway: a reader should
never need minus80, or a wrapper of ours, to open the store. For gzip-
compressed HDF5 the store needs nothing beyond zarr-python to read: its codecs
are Zarr's own and numcodecs' `zlib`. Check the codecs of every array before
publishing, because shadowzarr writes its own `keep_bytes` codec for arrays
whose bytes are interleaved with others', which only shadowzarr decodes.

### Chunk flat binary along the outer axis

In a C-ordered raw array, only the slowest-varying axis gives one contiguous
byte range per chunk, so `minus80.core.builders.flat_binary` chunks along it. A
small window inside a plane then reads whole slabs, so random access to small
full-resolution windows needs a copy.

A long 1-D column, such as a spike-time `.npy` with 21 million rows, would get
one chunk per element that way. Pass `rows_per_chunk` to group rows into
slabs: an uncompressed file has no storage unit of its own, so any byte range
is a chunk that needs no decoding. Zarr cannot decode a short last chunk from
a reference, so write the rows past the last full chunk natively, from the
bytes `flat_tail` names. Size the slab against that native remainder: 2^18
rows of float64 leaves at most 2 MB per column.

### Reference the base level and write derived levels natively

Icechunk can mix virtual and native chunks in one repo. For a source with no
pyramid, keep level 0 virtual and write downsampled levels natively with
ngff-zarr, through `minus80.core.multiscale`. Level 0 holds most of a pyramid's
bytes, so referencing it saves most of the storage.

Price each level before writing it. OME-Zarr requires only that levels run from
largest to smallest, so 2× steps are optional: over a 384-well plate a 2×
ladder costs about 1.9 GB, and the single 16× level that a whole-plate view
renders costs 23 MB.

### Prefer codecs every Zarr client can read

The store keeps the source's compressed bytes, so a reader needs the source's
codec.

- Readable from any language: raw bytes, gzip and zlib, zstd, blosc.
- Readable in practice only from Python: JPEG, JPEG 2000 and JPEG-XR through
  `imagecodecs`. Zarr v3 has no registered name for them, so each store
  invents one.

A non-Python client cannot read a JPEG or LZW level 0, so write the derived
levels with zstd and that client still gets the overview. Name the codecs the
store needs in its root attributes. Drop a bespoke codec once a maintained one,
such as `flac-numcodecs`, decodes the format.

## Indexing remote files

### Price indexing before running it

Indexing cost is the number of round trips through the metadata chain, not the
number of bytes. A 131 MB stack with a 658-page directory chain takes 190 s to
index from a laptop, and 2.7 s to download whole.
`minus80.parsers.tiff.inspect.indexing_cost` measures both strategies.

### Index a well-chunked HDF5 file in place

The cost of indexing HDF5 remotely is set by the number of index nodes, not by
the file's size. A 92 GB IBL NWB file written by NeuroConv, with 10 MB chunks,
indexes one probe's 9,000 chunks in 189 requests and 12 MB, in 29 s from a
laptop. Downloading the file to index it, as `minus80.parsers.nwb` does for a
remote source, would move 92 GB. Count the chunks first: a file with millions
of small chunks has an index worth downloading, and one with thousands does
not. Pass `HDFParser` a block reader (`reader_factory`) with 64 KB blocks; the
block size barely matters when the walk is bound by round trips. shadowzarr's
`generate_rfs` reads the nodes of each index ahead, a level at a time and in
parallel, and maps the same whole file, both probes, in 7 s.

A file with hundreds of thousands of small chunks is still worth indexing in
place when the reader walks its index in parallel. A 71 GB MICrONS NWB file
holds a stimulus movie in 381,054 one-frame chunks. From a laptop, h5py
visiting the file's 364 object headers alone took 761 s and 8,049 requests;
`HDFParser`, one group at a time, took 569 s, 476 of them for the movie; and
`generate_rfs` mapped the whole file, the movie's index included, in 59 s.

### Probe the layout before walking it

A TIFF's directories form a linked list, but writers often lay them out at a
constant stride. `minus80.parsers.tiff.inspect.directory_layout` checks for that layout
in four requests. When it holds, fetch the directories in bulk: for 829,080
directories in one 168 GB OME-TIFF, that takes about 1.6 minutes instead of 40.
When it does not, walk the chain.

A file can hold its first directory apart from the rest. tifffile writes a
contiguous series with the first directory at byte 16, before the pixels,
and every other directory after them, so a region that started at the first
directory would take in every pixel: 70 GB for one MICrONS scan instead of
75 MB. The region starts at the second directory, and the first comes with
the file's first 64 KB.

### Separate fetching from parsing

A parser written for local files seeks freely, which costs nothing locally and
a round trip per seek over HTTP. The 829,080 directories above parse in 18 s;
the rest of the 40 minutes is waiting. Fetch the metadata regions into a sparse
local file at their true offsets (`minus80.core.fetch.sparse_local`) and let
the parser seek against that file. Otherwise download the file once with
parallel range requests (`minus80.core.fetch.download_then_index`) and keep
referencing the remote URL.

### Budget CPU for the download as well as the parse

A scan that reads every byte of every file uses CPU for the download, not only
for the parse. TLS, HTTP and the kernel's network interrupts all cost CPU time.
Size the VM for the sum of both costs, and measure the parse rate on the VM
type you will use, because a laptop's rate does not carry over.

As an example, consider a FLAC frame scan of SanctSound on Coiled c6in VMs in
us-east-1, reading from a Google Cloud bucket (measured 2026-11-09):

- Downloading cost about 3.3 CPU-seconds per GB.
- Parsing with PyAV cost about 3 CPU-seconds per GB. One vCPU parsed about
  450 to 520 MB/s, while one Apple laptop core parsed 1.5 GB/s.
- The network delivered 1.7 GB/s. At about 6.5 CPU-seconds per GB in total,
  that rate needs about 11 busy vCPUs.

On a c6in.2xlarge (8 vCPUs), every read pattern that was tried plateaued
between 780 and 840 MB/s. Run whole-file scans on a c6in.4xlarge (16 vCPUs) or
larger.

Hold each downloaded file in memory rather than in `/dev/shm` or on disk.
Coiled's container has a 64 MB `/dev/shm`, and `coiled batch run` cannot
enlarge it. Writing downloads to the VM's root volume ran at 210 to 520 MB/s,
well under the network's rate.

### Check every member a copy reads

Bytes can arrive damaged. Copying S-BIAD3228 from EBI, three zip members in
30 wells failed their CRC-32 check on the first read and were good on the
second. Check each member against the checksum the format records, read a
failing one again, and name it when it fails twice. A damaged member that is
not checked becomes wrong pixels or looks like a corrupt source.

### Keep many ranges asked for ahead on a slow host

Some hosts have slow spells that hold every request. On EBI's FTP server,
single 64 MiB ranges took from 53 to 309 s, and the same range asked for a
second time was just as slow. A second request does not escape such a spell;
keeping many ranges asked for ahead of the parser does, for as long as the
spell is shorter than the window. For S-BIAD3228, 64 ranges (4 GiB) covered
about 30 s.

## Testing

### Test with a real file from the archive

A generated file exercises the encoder's defaults, not the format, and tends to
leave out whatever the spec makes optional. FLAC written by `libsndfile` has no
`SEEKTABLE`, but archive files have one with uneven intervals, which shifted a
regular chunk grid by 11.5 s over a 2-hour file without an error. Use a real
file from the target archive as the primary fixture.

## Library traps

| Trap | Fix |
| --- | --- |
| obstore re-encodes `%20` as `%2520` and gets a 404 | `minus80.core.registry.store_and_key` decodes once |
| obspec-utils' registry keeps a store's prefix in the key when a folder name needs percent-encoding (`#2024-05-06#`, a space, `+`), so the prefix is requested twice and 404s | `minus80.core.registry.key_within` compares decoded with decoded |
| A `#` left unencoded in a URL starts its fragment, and everything after it is dropped from the key | write `%23`; Icechunk reads a virtual chunk whose URL holds `%23` |
| `obstore.from_url` signs requests to S3 hosts and fails off EC2 | a plain `HTTPStore` for anonymous reads |
| `s3://idr/` fails TLS with virtual-host addressing | `force_path_style` |
| Icechunk refuses virtual reads from an unauthorized prefix | `minus80.core.icechunk_io.open_repo` authorizes the prefixes in the repo config |
| `create_repo` takes S3 region and anonymity from the build shell's environment | set both for the source bucket, and read the repo back from a clean environment |
| ngff-zarr calls `consolidate_metadata`, which Icechunk rejects | `minus80.core.multiscale` disables it during the write |
| Default zstd barely compresses real timestamps | blosc shuffle with zstd on every coordinate |
| Time steps accumulated by addition drift by hundreds of ms | compute each from its index: `i * 1e9 // rate` ns |
| A download cache keyed on basename serves one file's bytes for another | `minus80.core.fetch` keys on the full URL and renames atomically |
| `arraylake` reads `ARRAYLAKE_TOKEN` when first imported | set the token before importing |
| xarray `where(..., drop=True)` raises on `StringDType` columns | `sel` or boolean `isel` |
| A script named like a module (`attrs.py`) shadows it when run from its folder | name scripts so they cannot shadow a package |
| PyAV's Linux wheel (19.0.2) fails on HTTPS URLs with "Input/output error"; the macOS wheel works | download first and hand PyAV bytes or a local path |
| PyAV demuxing slows down as threads are added: 1.1 GB/s on one thread, 125 MB/s on eight | parse in spawned processes |
| Forking a process after obstore has started its threads can deadlock | `multiprocessing.get_context("spawn")` |
| Coiled's container has a 64 MB `/dev/shm` | hold downloads in memory |
