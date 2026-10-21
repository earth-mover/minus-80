# Cross-format gotchas

Hard-won knowledge that recurs across bio formats. Read before starting a new one;
add to it when a new format teaches you something. (Format-specific findings stay
in each subproject's `FINDINGS.md`; this is what generalizes.)

## What blocks virtualization outright

- **Whole-file gzip/DEFLATE** (`.nii.gz`, `.mgz`, `.fastq.gz`, `.mrc.gz`): DEFLATE
  back-references are sequential — a byte range mid-stream is undecodable without
  inflating from the start. Byte-range virtualization is impossible. Only fix is a
  one-time transcode to uncompressed (or a BGZF-style blocked recompress).
- **Entropy-coded / RLE payloads with no fixed offsets** (EER electron events,
  LZW-TIFF movie frames): even if you can point at the blob, chunk boundaries
  aren't at computable offsets. Decode is mandatory → no benefit.
- **Ragged record streams in block-gzip** (BAM/CRAM/VCF): blocks are byte-indexed
  but records span blocks and don't align to array cells. This is a *table*, not a
  dense array — convert (vcf2zarr) or serve dynamically (htsget); don't virtualize.
- **Interval-encoded arrays** (bigWig, bigBed, and any coordinate-carrying record
  stream): byte-addressable and cleanly chunk-referenceable, yet still the wrong call.
  Each record stores its own start/end, so **the coordinate is in the payload and the
  chunk index means nothing**. Virtual chunks yield `(start, end, value)` triples that no
  array engine can select on by position, and two files never share a grid because their
  section boundaries follow their own data. Convert to a position-indexed array; it is
  also *smaller* (0.38x measured on ENCODE bigWig). This is not archetype D — the records
  are fixed-stride and decode fine. See `virtual-tracks/FINDINGS.md`.
- **Variable-length strings / categoricals** (HDF5 global heap: AnnData `obs`/`var`
  indices, NWB string columns): they live in the heap, not fixed-stride buffers, so
  they are not byte-range references. Virtualize the numeric leaves; load strings
  into memory or write them natively into the repo (makes it self-contained).

## Synthesized test files hide the bugs that matter

A file you generate exercises the *encoder defaults*, not the format. FLAC written
by `libsndfile` carries no `SEEKTABLE` and a uniform frame grid; real archive files
carry a `SEEKTABLE` whose intervals vary by a frame, which silently misaligns a
regular chunk grid (11.5 s of drift across one 2-hour file, with no error raised).
The same trap exists wherever an index is *optional* in the spec — the generator
omits it, real writers emit it, and your parser never meets the hard case.

Use a real file from the archive or instrument you are targeting as the primary
fixture. Synthetic files are fine for exercising error paths, never for validating
that a format virtualizes.

## The xarray dimension model vs. independent arrays

VirtualiZarr's `open_virtual_dataset` / `to_icechunk` funnel through an xarray
`Dataset`, which auto-names each array's first axis `phony_dim_0` and then refuses
conflicting lengths. But many bio structures are *independent* 1-D/N-D arrays that
have no business sharing a dimension:

- CSR sparse: `data`/`indices` (nnz) vs `indptr` (n_rows+1) — different lengths.
- Image pyramids: each level a different size.
- MRC volume + per-tilt metadata arrays.

**Fix used in core:** write with `ManifestGroup.to_icechunk` (VirtualiZarr#1108),
which writes each array with its own dimension names and never builds a Dataset,
so there is no shared-dim rule to break. (If you *do* need them as
one logical array across files — ragged concat — that's `virtual-h5ad`'s rectilinear
chunk-grid path; both are UPSTREAM.md candidates.)

## Padded final chunks

HDF5 (and some tiled formats) pad the *last* chunk of an array to full chunk size.
Concatenated across files, that padding leaks across the boundary and corrupts the
next file's data. Write each file's tail chunk natively holding only its valid
elements. This is the subtle fix that makes byte-identical roundtrips hold.

## Icechunk virtual-chunk authorization

Icechunk refuses to read virtual chunks unless the source prefix is authorized
(`authorize_virtual_chunk_access`). Make repos self-describing: store the source
prefixes in config and read them back on open, so a reader needs no prior knowledge
of where the data lives. One `VirtualChunkContainer` per distinct source prefix.

## Indexing remote files

Walking a file's internal metadata (HDF5 B-tree, TIFF IFD chain, MRC extended
header) over HTTP is thousands of latency-bound round trips. Pull the file once
with `fetch.download_then_index` (parallel range GETs), parse locally, but keep
*referencing* the remote URL in the manifest. The local copy is scratch.

## Flat-binary: partial reads are cheap, but only along the contiguous axis

Raw uncompressed data is the *best* case for partial access — no decode state, any
byte range is immediately valid (unlike gzip's sequential back-refs or a JPEG tile
that must be fully decoded). BUT for a C-ordered array only the **slowest-varying
(outer) axis** yields one contiguous byte range per chunk, so `flat_binary` chunks
along axis 0. Consequence:

- Outer-axis subset (z-planes, frames, particles) → one range GET per slab, cheap,
  coalescable into one.
- Inner-axis subset (a small x,y AOI in a plane) → the chunk model reads **whole
  slabs**; an inner window is strided in the file, not a single range.

If true full-res random AOI matters, you must re-chunk (materialize) — plain
virtualization can't give N-D chunk granularity over a contiguous blob.

## Hybrid virtual + native multiscale (the pattern that makes imaging worth it)

Icechunk can mix virtual and native chunks in one repo. For a format with no
internal pyramid (MRC, raw volumes) — or when you want cloud-native multiscale
without copying the big data:

- **Level 0 (full-res): virtual** — references the source bytes, no copy. In a 3D
  pyramid this is ~88% of the total size (1 + ⅛ + ¹⁄₆₄ + … ≈ 1.14× L0), so this
  alone saves the overwhelming majority of storage.
- **Levels 1…N (downsampled): native** — computed once, tiny, written as real
  chunks alongside the virtual L0.

You get responsive multiscale visualization AND avoid copying the expensive level.
This is the honest answer to "why virtualize a cloud-native-ish format": you don't
virtualize the derived pyramid, you virtualize the base and materialize the cheap
levels.

**The levels are cheap to store and expensive to build.** Deriving them reads
every full-resolution pixel once, and that read is the whole base array — for a
384-well Cell Painting plate, 65 GB and ~7 minutes in-region against 47 s to
index the same plate. So the derived layer, not the indexing, is what a batch
run has to parallelize, and it is the one place the store stops being pure
references (`virtual-cellpainting/README.md` "Tradeoffs").

**How many levels is a measurement, not a convention.** OME-Zarr requires only
that levels run largest to smallest; 2× steps are a habit, not a rule. A 2×
ladder over that same plate costs ~1.9 GB against 0.27 MB of references, nearly
all of it in level 1 — a half-resolution copy of an image that already loads
instantly. One 16× level is 23 MB, 0.3% of the pixel volume, and is the
resolution a whole-plate view renders at anyway. Price the ladder per level
before writing one.

**When level 0's codec is Python-locked, the native levels are the only way
out.** A virtual base referencing LZW, JPEG or JPEG 2000 bytes is unreadable to
a non-Python Zarr client (the portability tiers are under "Decode-library licensing + codec
portability"), but the
derived levels are written in whatever codec you choose. Give them a portable
one — zstd — and a cross-language reader gets the overview even though it can
never get full resolution.

**Buy the pyramid, don't build it.** Downsampling + OME-Zarr `multiscales` metadata
is `ngff-zarr` (or `ome-zarr-py`) — do NOT hand-roll `downsample_2x` or hand-author
multiscales attrs (copick's `util/ome.py` is a roll-your-own; don't copy it). The
ONLY novel piece is keeping level 0 **virtual** (flat_binary references) inside an
otherwise-native NGFF pyramid — a `ngff-zarr` × `virtualizarr` integration, and an
UPSTREAM.md candidate, not a private reimplementation.

## Benchmark read TIME under remote conditions, never locally

Report the wall-clock **read time a user experiences**, measured under realistic
remote latency + bandwidth — via snailmail's injected latency/bandwidth, or against
the real remote store. **Never headline a local time**: locally everything is
memory-bandwidth-bound, so virtualization only adds indirection and reads *slower* —
which says nothing about the remote case that matters. And **don't report GET counts**
— that's internal mechanism, not what the user feels; report seconds.

Compare the virtual read against the realistic alternative — **fully downloading the
file(s)** (or opening each the normal way) and reading. Under latency the virtual path
reads lazily and skips re-reading each file's internal metadata, so it's faster:

- **Single flat-binary file** (MRC): the alternative is small (header + one read), so
  the virtual win is modest — say so honestly.
- **Collections**: the alternative downloads/opens N files; the virtual read goes
  straight to only the bytes you asked for across the whole set. This is the real
  story — always show the collection case, in seconds.
- **HDF5** (h5ad/NWB): biggest win even for one file — reading its scattered internal
  metadata over the network is slow, and the manifest skips it entirely.

`virtual-h5ad/latency_benchmark.py` is the reference pattern: virtualize against a
snailmail URL and time the read under injected latency.

## Decode-library licensing + codec portability

The virtual store keeps the source *compressed* bytes and records a codec to decode
them at read time — so the decode library's license and cross-language availability
travel with the dataset.

- **Licenses are all permissive** across our decode/parse stack — BSD-3 (imagecodecs,
  tifffile, czifile, mrcfile, starfile, pynwb, h5py), MIT (zarr, numcodecs, obstore,
  ngff-zarr), Apache-2.0 (virtualizarr, icechunk). No GPL/LGPL in the read path.
- **`imagecodecs` is BSD-3 but bundles many C codecs** (libjpeg, openjpeg, jxrlib,
  zstd…) with their own (mostly BSD/MIT) licenses; JPEG-XR/JPEG2000 patents have
  expired. Fine to depend on; if you redistribute wheels, the bundled licenses travel.
- **Portability is the real constraint, not licensing.** "Readable by any Zarr client
  incl. non-Python" holds only for codecs with cross-language implementations:
  - *Portable everywhere:* raw `bytes` (flat-binary: MRC, ephys), gzip/zlib (HDF5:
    h5ad/NWB), zstd (CZI-zstd), blosc.
  - *Python-only in practice:* the `imagecodecs`-backed image codecs — JPEG (NDPI/WSI),
    JPEG-XR, JPEG2000. Those stores are permissively licensed but effectively
    Python-locked at read time. Prefer these formats' uncompressed/zstd variants when a
    cross-language store matters, and say so in the subproject's Tradeoffs section.

## Endianness & data offset

Flat-binary formats declare endianness and a data-start offset in their header
(MRC: 1024 + extended-header length; NIfTI: `vox_offset`). Read these with the
format library, never assume. Pass endianness into the zarr `bytes` codec.
