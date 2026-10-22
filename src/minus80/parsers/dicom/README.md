# minus80.parsers.dicom

DICOM pixel data as VirtualiZarr arrays whose chunks reference the frames in the
original instances. pydicom reads the headers; this parser works out where each
frame's bytes sit and names the codec that decodes them.

```bash
uv sync --extra dicom
```

Each frame of an instance is one Zarr chunk, and one array can span many
instances: one per channel, per slice, or per pyramid level.

```python
from minus80.parsers.dicom import wsi_levels

levels = wsi_levels(series_urls)          # {"0": ManifestArray, "1": ...}, largest first
levels["0"].metadata.attributes["scale"]  # {"y": 0.0002525, "x": 0.0002525}, in millimetres
```

A source is a URL (`s3://`, `https://`, `file://`), a local path, or an open
binary file. A file states no address, so a file source requires `url=`, the
address the manifest records. `url=` on a path or URL overrides the recorded
address too, for example to parse a local copy of a remote object.

| Function | Input | Returns |
| --- | --- | --- |
| `wsi_levels(sources, urls=, attributes=)` | The instances of one whole-slide series | Its `VOLUME` levels, keyed `"0"` (largest), `"1"`, …; overview, thumbnail and label instances are left out |
| `wsi_level_array(source, url=, attributes=)` | One tiled pyramid level | A `(c, y, x)` array for three samples, `(y, x)` for one, one chunk per tile |
| `multiplex_array(sources, urls=, attributes=)` | Single-frame instances, one optical path each | A `(c, y, x)` array ordered by `OpticalPathIdentifier`, with `channel_names` |
| `volume_array(sources, urls=, attributes=)` | Single-slice radiology instances | A `(z, y, x)` array ordered by `ImagePositionPatient` z, else `InstanceNumber`, with `z_positions` |
| `dicom_json(source)` | Any instance | The element tree as DICOM JSON (PS3.18), pixel data excluded, binary elements inline as base64 |
| `encapsulated_frame_ranges(source)` | A compressed instance | The file offset and length of each frame |
| `native_pixeldata_byte_range(source, n)` | An uncompressed instance | The file offset of the PixelData value, and `n` |

Every array carries `scale` and `units` (`"millimeter"`) for each axis whose
spacing the file states: `PixelSpacing` from the shared functional groups or the
top level, and for a volume the median step between slice positions. An axis
the file does not state has no scale. `attributes=` is merged over these.

A store written from these arrays reads with zarr, imagecodecs and
imagecodecs-zarr. It does not need a DICOM library.

## Indexing a remote level

A compressed level's frame offsets come from its Extended or Basic Offset
Table, which sit in the header, so indexing a remote level reads only the
header. An instance with neither table is downloaded once into
`minus80.core.fetch.cache_dir()` and walked locally. Many IDC slides have no
table; the measurements are in
[docs/parsers/dicom.md](../../../../docs/parsers/dicom.md#offset-tables).

## Limits

- **Transfer syntaxes.** Explicit and Implicit VR Little Endian everywhere;
  JPEG Baseline, JPEG Extended and JPEG 2000 for whole-slide levels only.
  Anything else raises `ValueError`.
- **Bits.** 8 or 16 bits allocated, all of them stored. An instance whose
  `BitsStored` is less than `BitsAllocated`, such as 12-bit CT in 16 bits,
  raises `ValueError`: pydicom masks or sign-extends the unused high bits, and a
  Zarr read would return them as stored.
- **Whole-slide levels** are one `TILED_FULL` instance each, with one fragment
  per frame. `TILED_SPARSE`, levels concatenated across instances, and a
  frame split into several fragments raise `ValueError`.
- **Multiplexed whole-slide series**, which store each channel's pyramid as its
  own instances, are refused by `wsi_levels`: pass one optical path's
  instances at a time. Stacking them into one `(c, y, x)` pyramid is not
  implemented.
- **Multiplex** reads single-frame instances, one optical path each.
- **Volumes** are single-frame slices; enhanced multi-frame CT and MR are not
  supported. The z scale is the median step, so a series with uneven spacing
  has a scale that matches only some of its slices; `z_positions` lists them all.
- **Lossy frames** are not byte-identical across decoders: two conformant
  JPEG 2000 decoders can differ by one level. Compare a read with imagecodecs'
  decode of the same frame.

[docs/parsers/dicom.md](../../../../docs/parsers/dicom.md) explains how the
byte ranges are found and lists the traps on the way.

## Tests

```bash
uv run pytest tests/parsers/dicom
```

The tests write small synthetic instances with `minus80.parsers.dicom.synthetic`
and read each array back through Zarr, against the pixels written or pydicom's
decode. One serves instances from a local HTTP server to check the remote path.
