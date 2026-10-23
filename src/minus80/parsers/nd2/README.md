# minus80.parsers.nd2

Turns the frames of an uncompressed Nikon **ND2** file into
[VirtualiZarr](https://virtualizarr.readthedocs.io) `ManifestArray`s whose
chunks reference the file's own bytes. The
[`nd2`](https://github.com/tlambert03/nd2) library parses the file; this
package does not decode an ND2 chunk itself.

```bash
uv sync --extra nd2
```

## Usage

```python
from minus80.parsers.nd2 import index_nd2

index = index_nd2("https://…/0X.nd2")      # a URL, a local path, or an open file with url=
index.positions        # 4 stage positions
index.frame_shape      # (3, 2048, 2048), as (c, y, x)

arrays = index.position_arrays()          # {"0": ManifestArray, "1": …}
stacked = index.stacked_array(attributes={"study": "idr0095"})
```

`index_nd2(source, *, url=None)` returns an `ND2Index`: what the file states
about itself, and the byte offset of every frame. `source` is a URL, a local
path, or an open binary file. `url` is the address the arrays record, and
defaults to the source's own. An open file has no address, so passing one
without `url` raises `ValueError`.

Three methods turn the index into arrays. Each takes `attributes=`, which is
merged over the attributes the parser sets.

| Method | Returns | Shape | A chunk is |
| --- | --- | --- | --- |
| `position_array(p)` | one `ManifestArray` | `(c, y, x)` | the whole frame |
| `position_arrays()` | `{"0": …, "1": …}`, one per position in file order | `(c, y, x)` | the whole frame |
| `stacked_array()` | one `ManifestArray` | `(position, c, y, x)` | one frame |

The stacked array references the same bytes as the position arrays, so writing
both adds references and stores no extra pixels. To write any of them to
Icechunk, put it in a `ManifestGroup` and call `to_icechunk`.

Every array carries these attributes:

- `channel_names`, in the file's channel order;
- `scale` and `units` for `y` and `x` in micrometer, for each axis the file
  marks as calibrated;
- `source_url`, the file the manifest references.

A position array also carries `stage_position_um`, the stage's `[x, y, z]` as
the file states it. It is not turned into an OME-Zarr `translation`, because
the stage's axes can differ from the image's in orientation and sign.

An ND2 frame is stored `(y, x, c)`, with channels interleaved. The arrays are
declared `(c, y, x)` and decode through Zarr v3's `transpose` codec, so the
channel axis leads without rewriting a byte. The tests check every position
against `nd2.ND2File.asarray()`.

## Remote files

An ND2 ends with a chunk map: a table of `name -> (offset, size)` for every
chunk in the file, frames included, so the tail alone gives the offset of
every frame and the parser never walks the file frame by frame. Indexing a
101 MB file from `ftp.ebi.ac.uk` makes four requests and downloads 685 KB:

- a HEAD for the length;
- a 64 KiB block from the start and a 512 KiB block from the end;
- one range covering the metadata chunks between them, merged across gaps of
  up to 1 MiB, since a megabyte of padding costs less than another round trip.

`nd2.ND2File` reads by seeking a file, so these ranges are written into a
temporary sparse file of the source's length, and `nd2` reads that. No frame
byte is fetched. `index.requests` and `index.fetched_bytes` report the cost,
and are 0 for a local path or an open file, which `nd2` reads in place.

## Limits

- **Compressed ND2 is refused** with `UnsupportedND2`. Only uncompressed frames
  are referenced.
- **Padded rows are refused.** A row occupying more bytes than its pixels need
  is not a Zarr chunk.
- **Only a stage-position loop is read.** Each frame becomes one position, so
  a file that also loops over time or z would have those frames presented as
  fields of view. Such a file raises `UnsupportedND2`.
- **A chunk spans every channel and the whole frame**, because that is the
  file's storage unit. Reading one channel or one tile reads the frame.
- **An uncalibrated axis has no scale.** `nd2`'s `voxel_size()` reports 1.0
  for it, which would read as a real micrometre, so `scale` lists only the axes
  the file marks as calibrated.
- **Frame offsets come from a private attribute** of `nd2`'s reader,
  `ND2File._rdr._frame_offsets`, since `nd2` has no public API for them.

## Tests

```bash
uv run pytest tests/parsers/nd2                                # read-back tests skip without the fixture
MINUS80_ND2_TEST_DOWNLOAD=1 uv run pytest tests/parsers/nd2    # fetch the 101 MB fixture once
```

The remote test fetches only the index, and skips when the host does not
respond. The other tests need the whole file, because they compare against
`nd2`'s own decode. It is cached in `tests/parsers/nd2/.cache/`, or in
`MINUS80_ND2_TEST_CACHE`, and reused on later runs.
