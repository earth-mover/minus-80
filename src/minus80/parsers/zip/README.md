# minus80.parsers.zip

Turns the members of a zip archive into the chunks of a virtual Zarr array, one
member per chunk.

The parser is for zips of other files, such as TIFF frames or safetensors, where
the caller lays out the array. For a zipped Zarr store (`.zarr.zip`), use
VirtualiZarr's `ZippedZarrParser` instead, which reads the store's own metadata.

A zip does not state a shape, dtype or codec for its members, so the caller
supplies them: the grid of member names, the array's shape and dtype, and the
codec that decodes one member.

The `zip` extra is empty, because the base dependencies cover the parser.

## Usage

```python
import numpy as np
from minus80.parsers.zip.members import member_array

url = "https://example.org/frames.zip"   # STORED members
members = np.array([f"frames/t{t:03d}.tif" for t in range(92)], dtype=object).reshape(92, 1, 1)
image = member_array(
    url, members,
    shape=(92, 1024, 1024), chunk_shape=(1, 1024, 1024), dtype="uint8",
    serializer={"name": "imagecodecs_tiff"}, dimension_names=("t", "y", "x"),
)
```

`members` is shaped like the chunk grid. Each entry is a member name, or `None`
for a grid position the archive has no file for, which reads as the fill value.
`serializer` is the codec that decodes one member's bytes, here a TIFF.

Every function's `source` is a URL, a local path, or an open binary file. An
open file has no address to record, so `member_array` raises `TypeError` unless
`url=` gives one. `url=` also lets the manifest name a different copy of the
archive than the one read, such as the remote original of a local copy.

| Module | Functions |
| --- | --- |
| `members` | `zip_index(source)` lists the members through VirtualiZarr's `parse_zip_index`. `member_array(source, members, ...)` builds the array; pass `index=` to reuse an index. `read_member(source, entry)` returns one member's bytes, inflating a Deflate member, and `inflate_prefix` inflates only the start of one. |
| `buffers` | Reads zip structures without the central directory. `walk_archive(source)` walks every local header in order; `end_of_central_directory`, `walk_local_headers`, `scan_local_headers` and `member_from_buffer` work on bytes already fetched. |
| `safetensors_ranges` | `tensor_ranges(data, file_offset)` gives each tensor's byte range in a safetensors file, including one stored inside a zip member. |

[docs/parsers/zip.md](../../../../docs/parsers/zip.md) explains when to walk the
local headers instead of reading the central directory.

## Limits

- Only STORED members can be chunks; any other method raises
  `NotImplementedError`. A Deflate member is a bare Deflate stream, and
  imagecodecs-zarr's `Deflate(raw=True)`, the one Zarr codec for it, cannot
  decode a chunk without being told its uncompressed size.
  `read_member`, `inflate_prefix` and `buffers` do read Deflate members.
- A zero-length member has no bytes to reference, so it becomes a missing chunk
  and reads as the fill value, as `None` does.
- `parse_zip_index` fetches the whole central directory in one request, which
  is too large for archives with millions of members. Use `walk_archive` there.
- `buffers` cannot step over a member whose sizes sit in a data descriptor after
  its data, and raises `ValueError` for one.
- `buffers` imports two private functions, because nothing public reads a ZIP64
  end record or local header: `zipfile._EndRecData` and VirtualiZarr's
  `_parse_zip64_extra`.
