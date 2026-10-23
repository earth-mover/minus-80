# minus80.parsers.tiff

Turns the levels of a TIFF series into VirtualiZarr arrays whose chunks are the
file's own tiles or strips. tifffile parses the file; the parser restates each
level as a chunk manifest and names imagecodecs' Zarr 3 codecs to decode it. A
store written from it reads with icechunk, zarr, imagecodecs and
imagecodecs-zarr installed, and nothing else.

```bash
uv sync --extra tiff
```

## Usage

```python
from minus80.core import registry_for
from minus80.parsers.tiff import TiffParser

url = "s3://bucket/slide.ome.tif"
store = TiffParser()(url, registry_for(url))   # a ManifestStore
store.group["0"]                               # the full-resolution ManifestArray
```

`TiffParser(*, series=0, prefetch="ranges", attributes=None)` follows
VirtualiZarr's `Parser` protocol ([docs/parsers.md](../../../../docs/parsers.md)).
Calling it with a URL and an `ObjectStoreRegistry` returns a `ManifestStore`
over a `ManifestGroup` holding every resolution level of one series, keyed
`"0"`, `"1"`, … from full resolution down. Every read goes through the
registry, and the manifest records the URL as given.

- `series` is an index into tifffile's series or a series name.
- `prefetch` chooses how a remote file's directories are read; a local file is
  always read in ranges:
  - `"ranges"` walks them through a block-cached range reader;
  - `"directories"` fetches the header and the directory region into a sparse
    local file, when `inspect.directory_layout` finds the directories packed
    at the end of a BigTIFF;
  - `"whole"` downloads the file into the cache first.
- `attributes` are merged into every level's attributes.

Each page is one `(y, x)` grid of chunks. The series' other axes (`t`, `c`,
`z`) index pages, one chunk per page along each. Interleaved RGB becomes a `c`
axis in front of `y` and `x`. An axis tifffile cannot name, such as a plain
stack of pages, is left unnamed.

Each level carries what its own first page states:

- `dimensions`, each named axis's type: `t` is `time`, `c` is `channel`, and
  `z`, `y` and `x` are `space`. An unnamed axis has no entry;
- `scale` and `units`, in micrometers. On level `"0"` they come first from
  the OME-XML's `PhysicalSizeX`, `Y` and `Z`, then from ImageJ metadata (its
  `unit` applied to the resolution tags, and `spacing` for `z`). Any axis
  neither states, and every reduced level, takes `y` and `x` from the
  resolution tags when they carry a unit;
- `channel_names` of `["R", "G", "B"]` for an RGB or YCbCr page;
- `source_url`, the file the manifest references.

The group's attributes hold what the file states about the series, in the
file's own terms:

| Key | What it holds |
| --- | --- |
| `series` | tifffile's series: `index`, `name`, `kind` (such as `imagej` or `ome`), `axes` (such as `ZCYX`) and `shape` |
| `resolution` | the full-resolution page's `XResolution` and `YResolution`, each `[numerator, denominator]` pixels per unit, and `ResolutionUnit` by its TIFF name |
| `imagej` | the ImageJ metadata tifffile parses from the description, such as `slices`, `channels`, `spacing` and `unit`, for an ImageJ series |
| `ome_pixels` | the `PhysicalSize` attributes of the series' OME-XML `Pixels` element, for an OME series |

A key is absent when the file states nothing for it.

A tile or strip stored with zero bytes, and a page the series lists as absent,
become missing chunks, which read as the fill value 0.

## Before indexing: `inspect`

`minus80.parsers.tiff.inspect` answers questions about a TIFF, local or remote,
without indexing it. Each function returns data, and each has a subcommand.

| Function | Subcommand | What it answers |
| --- | --- | --- |
| `directory_layout(source)` | `layout` | Where the image file directories sit, and whether they are one contiguous array that can be fetched in a single read instead of walked one round trip at a time. |
| `codec_chain(source)` | `codecs` | The codec chain the parser would write for the first page, whether any codec is outside Zarr's v3 core, or why the parser refuses the page. |
| `indexing_cost(source)` | `cost` | The measured time to walk a sample of directories over range requests, extrapolated to the whole file. |
| `survey(prefix)` | `survey` | A listing of an HTTP index or local directory, with size and TIFF header per file. |

```bash
uv run python -m minus80.parsers.tiff.inspect layout s3://bucket/image.ome.tif
```

## Limits

The parser accepts only combinations it has checked tile for tile against
tifffile's own decode:

- compression: JPEG (with or without `JPEGTables`, RGB or YCbCr), LZW, or none;
- samples: one, or three interleaved;
- no predictor;
- tiles, or strips that divide the image height evenly.

Anything else raises `NotImplementedError` rather than writing references that
decode wrong.

A reduced level gets a `scale` only when its own page's resolution tags state
one; it is never derived from level 0's. OME-XML and ImageJ metadata describe
only the full-resolution image, so their sizes apply to level `"0"` alone. A
size in a unit tifffile does not recognise is dropped rather than read as
micrometers. ImageJ `Labels` label planes, not channels, so they become
`channel_names` only when every plane is a channel. Channel names in OME-XML
are not read yet.

The names `R`, `G` and `B` are a judgement call: the photometric tag states a
colour model, not channel names.

`prefetch="directories"` fills everything outside the fetched region with
zeros. It raises `ValueError` if a full page's tag lies outside the region, or
if a page's tile offsets all read as 0. The second check also refuses a page
with no stored tile at all; `"ranges"` reads that file.

## Bio TIFFs here, geospatial TIFFs in virtual-tiff

This parser is for microscopy and whole-slide TIFFs. For geospatial TIFFs,
including Cloud-Optimized GeoTIFFs, use
[virtual-tiff](https://github.com/virtual-zarr/virtual-tiff), which is built for
them.

virtual-tiff can't yet open most whole-slide files:

- It refuses JPEG tiles that share a `JPEGTables` tag ([`parser.py#L203`](https://github.com/virtual-zarr/virtual-tiff/blob/9d952019cc59432af86de827116365d03f0dddcf/src/virtual_tiff/parser.py#L203)),
  which slide scanners commonly use.
- It refuses YCbCr photometric interpretation ([`parser.py#L387`](https://github.com/virtual-zarr/virtual-tiff/blob/9d952019cc59432af86de827116365d03f0dddcf/src/virtual_tiff/parser.py#L387)),
  which is common in scanner JPEG.
- It reads metadata through async-tiff's readahead cache, which holds one
  contiguous block from byte 0 ([`cache.rs#L99`](https://github.com/developmentseed/async-tiff/blob/7dd6c80062c2f8750bf8b581c6860bce4e37fd94/src/metadata/cache.rs#L99)). When a file's
  directories are spread through it, reaching the last one fetches nearly every
  byte before it ([async-tiff#344](https://github.com/developmentseed/async-tiff/issues/344)).
- It wraps numcodecs v2 codecs, so its stores don't open with `imagecodecs-zarr`
  alone ([virtual-tiff#109](https://github.com/virtual-zarr/virtual-tiff/issues/109)).
- It turns each directory into its own array, so resolution levels aren't
  grouped into a pyramid. tifffile's `series` does that grouping for vendor
  formats.

The goal is to fix these in virtual-tiff and async-tiff, then retire this parser.
The `JPEGTables` and YCbCr refusals have not been filed upstream yet.
