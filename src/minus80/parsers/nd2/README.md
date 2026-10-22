# minus80.parsers.nd2

Address the frames of a Nikon **ND2** file where they already lie, as
[VirtualiZarr](https://virtualizarr.readthedocs.io) references into
[Icechunk](https://icechunk.io). No pixel is copied and no ND2 chunk is parsed
here: the parsing is [`nd2`](https://github.com/tlambert03/nd2)'s.

```bash
uv sync --extra nd2
```

## Why it is cheap

An ND2 ends with a **chunk map**: a flat table of `name -> (offset, size)`
covering every chunk in the file, image frames included. A reader that can
fetch the tail therefore addresses the whole file arithmetically, with no walk
from one frame to the next. Measured against `ftp.ebi.ac.uk`, one file costs
**four requests and about 685 KB**, whatever the file weighs:

| | |
| --- | --- |
| Requests | a HEAD, a head block, a tail block, one coalesced metadata range |
| Bytes fetched | 685 KB for a 101 MB file |
| Pixel bytes fetched | none |

## The one function

```python
from minus80.parsers.nd2 import index_nd2

index = index_nd2("https://…/0X.nd2")       # a URL, or a local path
index.positions        # 4 stage positions
index.frame_shape      # (3, 2048, 2048) — (c, y, x)
index.channel_names    # ('Phase', 'Tx Red', 'YFP'), in the file's own order
index.pixel_size_um    # {'y': 0.064468…, 'x': 0.064468…}

marr = index.position_array(0)             # a VirtualiZarr ManifestArray
```

`index_nd2(url)` returns an `ND2Index`: everything the file states about itself,
plus the byte offset of every frame. Two methods turn that into VirtualiZarr
`ManifestArray`s, which a `ManifestGroup` writes to a store with
`to_icechunk`:

| Method | Shape | A chunk is |
| --- | --- | --- |
| `position_array(p)` | `(c, y, x)` | the whole frame |
| `stacked_array(attrs=…)` | `(position, c, y, x)` | one frame |

Both address the same bytes, so writing both costs references and no storage.

**Chunking is never chosen here.** A chunk of the store is a chunk of the
source, so no read of these arrays asks the server for a range the file does
not already have.

**The channel axis leads without moving a byte.** An ND2 frame is stored
`(y, x, c)`, channel-interleaved, but Zarr v3's `transpose` codec is an
array-to-array codec that declares exactly that relation. The arrays are
spec-shaped `(c, y, x)` over untouched bytes, with codecs
`[transpose(order=(1, 2, 0)), bytes]`. The tests check this byte for byte
against `nd2.ND2File.asarray()`.

## Limits

- **Compressed ND2 is refused.** `UnsupportedND2`, because no Zarr codec
  decodes ND2's compression, so its bytes cannot be referenced.
- **Padded rows are refused.** A row occupying more bytes than its pixels need
  is not a Zarr chunk.
- **Only a stage-position loop is read.** A frame becomes one position, so a
  file that also loops over time or z would have those frames presented as
  fields of view. Such a file raises `UnsupportedND2` rather than being
  described wrongly.
- **A chunk spans every channel**, so reading one channel reads all of them.
  Tiling within a frame is not available either: the file's chunk is the frame.
- **No z scale is reported.** `nd2` answers 1.0 for a file with no z loop,
  which is a placeholder for an absent axis rather than a micrometre, so
  `pixel_size_um` carries `y` and `x` only.

## Tests

```bash
uv run pytest tests/parsers/nd2                                # byte-identity tests skip without a fixture
MINUS80_ND2_TEST_DOWNLOAD=1 uv run pytest tests/parsers/nd2    # fetch the 101 MB fixture once
```

The remote test reads only the index, so it is quick over any connection, and
it skips when the host is not answering: a server that is down is not a failure
of this parser. The byte-identity test needs the whole file, because `nd2`'s
reader takes a path. It caches the download in `tests/parsers/nd2/.cache/`
(or `MINUS80_ND2_TEST_CACHE`) and reuses that copy on every later run, so the
rest of the suite needs no network at all.
