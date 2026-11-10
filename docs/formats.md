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

### Chunk flat binary along the outer axis

In a C-ordered raw array, only the slowest-varying axis gives one contiguous
byte range per chunk, so `minus80.core.builders.flat_binary` chunks along it. A
small window inside a plane then reads whole slabs, so random access to small
full-resolution windows needs a copy.

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

### Say when a bucket is public

Anonymous reads from a public S3 bucket need `MINUS80_S3_ANONYMOUS=1` in the
environment; without it, obstore and icechunk sign every request and the read
fails.

### Probe the layout before walking it

A TIFF's directories form a linked list, but writers often lay them out at a
constant stride. `minus80.parsers.tiff.inspect.directory_layout` checks for that layout
in four requests. When it holds, fetch the directories in bulk: for 829,080
directories in one 168 GB OME-TIFF, that takes about 1.6 minutes instead of 40.
When it does not, walk the chain.

### Separate fetching from parsing

A parser written for local files seeks freely, which costs nothing locally and
a round trip per seek over HTTP. The 829,080 directories above parse in 18 s;
the rest of the 40 minutes is waiting. Fetch the metadata regions into a sparse
local file at their true offsets (`minus80.core.fetch.sparse_local`) and let
the parser seek against that file. Otherwise download the file once with
parallel range requests (`minus80.core.fetch.download_then_index`) and keep
referencing the remote URL.

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
