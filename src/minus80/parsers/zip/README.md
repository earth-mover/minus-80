# minus80.parsers.zip

Zip archive members as virtual Zarr chunks, one member per chunk.

The parser is for zips of other files, such as TIFF frames or safetensors,
where the caller lays out the array. For a zipped Zarr store (`.zarr.zip`), use
VirtualiZarr's `ZippedZarrParser` instead, which reads the store's own metadata;
when its members are Deflate-compressed and must be read rather than
referenced, open it with `store.MemberStore`.

Only STORED (uncompressed) members can be chunks. A Deflate member is a bare
Deflate stream, and imagecodecs-zarr's `Deflate(raw=True)`, the one Zarr codec
for it, cannot yet decode a chunk without being told its uncompressed size.
Once it can, Deflate members become chunks too, and the member-per-chunk layout
belongs upstream beside `ZippedZarrParser`, which lists it as future work.

| Module | What it does |
| --- | --- |
| `members` | `zip_index(url)` lists an archive's members; `member_array(...)` builds an array with one member per chunk; `read_member(url, entry)` fetches one member's bytes, inflating a Deflate member |
| `store` | `MemberStore` opens a zipped Zarr store in place for zarr to read: each key is fetched from its member by one range request and inflated on a worker thread, so zarr's concurrent reads stay concurrent. For reading the pixels of a `.zarr.zip` whose members are Deflate-compressed, which cannot be referenced virtually; its docstring says why zarr's `ZipStore` and fsspec's zip filesystem are slower for this |
| `structures` | Parsing, bytes only, standard library only: the end records (`end_of_central_directory`, through `ArchiveTail`), a local header (`parse_local_header`, with `name_encoding` and `zip64_values`), and `inflate`. Every other module parses these structures through it |
| `directory` | Parsing, bytes only: `parse_central_directory(archive_end, size)` turns the end of an archive into a table, one row per member with its local-header offset, sizes, method and CRC (parsed by `zipfile`); `member_spans(table, names=None)` gives each member's span from its local header to the next; `parse_member` reads one member from its span, checking the CRC. For copying an archive, not referencing it |
| `reading` | Fetching (obstore, through `minus80.core.fetch`), then parsing with `directory`: `read_central_directory(url, registry=)` and `cached_central_directory` (kept as Parquet, checked against the archive's ETag, last-modified time or size); `read_members` fetches a few members, one request each |
| `buffers` | Reads members from bytes already fetched, without the central directory: the members in a window, and `walk_archive`, which walks a whole remote archive in order |
| `safetensors_ranges` | `tensor_ranges(data, file_offset)` gives each tensor's byte range in a safetensors file, including one stored inside a zip member |

The `zip` extra adds `pyarrow`, which `directory` and `reading` use to keep the central
directory as a Parquet file. Everything else needs only the base dependencies.

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
- `structures` uses one private function, `zipfile._EndRecData`, because
  nothing public reads a ZIP64 end record. `end_of_central_directory` takes the
  archive's size because recent Python patch releases make `_EndRecData` check
  the ZIP64 locator's absolute offset.
- `parse_zip_index` fetches the whole central directory in one request, which
  is too large for archives with millions of members. Use `buffers` there.
