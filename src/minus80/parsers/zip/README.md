# minus80.parsers.zip

Zip archive members as virtual Zarr chunks, one member per chunk.

The parser is for zips of other files, such as TIFF frames or safetensors,
where the caller lays out the array. For a zipped Zarr store (`.zarr.zip`), use
VirtualiZarr's `ZippedZarrParser` instead, which reads the store's own metadata.

Only STORED (uncompressed) members can be chunks. A Deflate member is a bare
Deflate stream, and imagecodecs-zarr's `Deflate(raw=True)`, the one Zarr codec
for it, cannot yet decode a chunk without being told its uncompressed size.
Once it can, Deflate members become chunks too, and the member-per-chunk layout
belongs upstream beside `ZippedZarrParser`, which lists it as future work.

| Module | What it does |
| --- | --- |
| `members` | `zip_index(url)` lists an archive's members; `member_array(...)` builds an array with one member per chunk; `read_member(url, entry)` fetches one member's bytes, inflating a Deflate member |
| `buffers` | Reads zip structures from bytes already fetched, without the central directory: the end record from an archive's tail, the members in a window, and `walk_archive`, which walks a whole remote archive in order |
| `safetensors_ranges` | `tensor_ranges(data, file_offset)` gives each tensor's byte range in a safetensors file, including one stored inside a zip member |

The parser needs nothing beyond the base dependencies, so the `zip` extra is
empty.

## Example

```python
import numpy as np
from minus80.parsers.zip.members import member_array, zip_index

url = "https://example.org/frames.zip"   # STORED members
index = zip_index(url)
members = np.array([f"frames/t{t:03d}.tif" for t in range(92)], dtype=object).reshape(92, 1, 1)
image = member_array(
    url, index, members,
    shape=(92, 1024, 1024), chunk_shape=(1, 1024, 1024), dtype="uint8",
    serializer={"name": "imagecodecs_tiff"}, dimension_names=("t", "y", "x"),
)
```

`members` is shaped like the chunk grid. Each entry is a member name, or `None`
for a grid position the archive has no file for, which reads as the fill value.
`serializer` is the codec that decodes one member's bytes, here a TIFF.

## What it buys

- **Indexing** comes from VirtualiZarr's `parse_zip_index`, which reads the end
  record (ZIP64 included), the central directory, and the local headers when the
  directory's offsets cannot be trusted. It reads no member data.
- **Decoding** is the serializer's own codec. STORED members need nothing else.

## Limits

- Only STORED members can be chunks; any other method raises
  `NotImplementedError`. `read_member` and `buffers` also read Deflate members.
- A zero-length member has no bytes to reference, so it becomes a missing chunk
  and reads as the fill value, as `None` does.
- `buffers` cannot step over a member whose sizes sit in a data descriptor after
  its data, and raises `ValueError` for one.
- `buffers` imports two private functions, because nothing public reads a ZIP64
  end record or local header: `zipfile._EndRecData` and VirtualiZarr's
  `_parse_zip64_extra`. `end_of_central_directory` takes the archive's size
  because recent Python patch releases make `_EndRecData` check the ZIP64
  locator's absolute offset.
- `parse_zip_index` fetches the whole central directory in one request, which
  is too large for archives with millions of members. Use `buffers` there.
