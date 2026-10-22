# minus80.parsers.tiff

TIFF pages as VirtualiZarr arrays, built from what tifffile has already parsed.

```bash
uv sync --extra tiff
```

tifffile walks the file and records where every tile's bytes sit. This parser
restates a page as a chunk manifest, one chunk per tile or strip, and names
imagecodecs' own Zarr 3 codecs to decode them. A store written from it reads
with icechunk, zarr, imagecodecs and imagecodecs-zarr installed, and nothing
else.

```python
import tifffile
from minus80.parsers.tiff import series_arrays

tf = tifffile.TiffFile(open_the_file_somehow)
levels = series_arrays(tf.series[0], "s3://bucket/slide.tif")  # {"0": ManifestArray, ...}
```

| Function | What it returns |
| --- | --- |
| `page_array(page, url, shape=None)` | One page as a `ManifestArray`, `(c, y, x)` for interleaved RGB, `(y, x)` otherwise. `shape` overrides the page's own when tifffile reports a trimmed level shape. |
| `series_arrays(series, url)` | Every level of a tifffile series, keyed `"0"`, `"1"`, … |
| `page_codecs(page)` | The Zarr codec chain for one page's tiles. JPEG colour spaces come from tifffile's own `jpeg_decode_colorspace`. |
| `page_tags(page)` | Every tag but the tile locations and JPEG tables, as JSON. |
| `resolution_um(page)` | The (y, x) pixel size the resolution tags state, or `None` unless all three tags are present. |

A tile or strip stored with zero bytes becomes a missing chunk, read as the
fill value 0.

## Before indexing: `inspect`

`minus80.parsers.tiff.inspect` answers questions about a remote TIFF without
indexing it. Each function returns data; each also has a subcommand.

| Function | Subcommand | What it answers |
| --- | --- | --- |
| `directory_layout(url)` | `layout` | Where the image file directories sit, and whether they are one contiguous array that can be fetched in a single read instead of walked one round trip at a time. |
| `codec_chain(url)` | `codecs` | Which codecs a page needs, and whether any is outside Zarr's v3 core. |
| `indexing_cost(url)` | `cost` | The measured time to walk a sample of directories over range requests, extrapolated to the whole file. |
| `survey(prefix)` | `survey` | A listing of an HTTP index or local directory, with size and TIFF header per file. |

```bash
uv run python -m minus80.parsers.tiff.inspect layout s3://bucket/image.ome.tif
```

## Limits

Only combinations checked tile for tile against tifffile's own decode are
accepted:

- compression: JPEG (with or without `JPEGTables`, RGB or YCbCr), LZW, or none;
- samples: one, or three interleaved;
- no predictor;
- tiles, or strips that divide the image height evenly.

Anything else raises `NotImplementedError` rather than writing references that
decode wrong.

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
