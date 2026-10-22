# minus80.parsers.dicom

DICOM pixel data as VirtualiZarr arrays that reference the bytes in the
original instances. [pydicom](https://pydicom.github.io) parses the element
tree; this parser only works out where each frame's bytes sit and restates them
as a chunk manifest.

```bash
uv sync --extra dicom
```

DICOM is an archive format, not a chunked array format: a dataset arrives as
many instances, one file each, and a reader has to decode a whole instance to
see any of it. Each frame in an instance is already a contiguous run of bytes,
though, so each frame becomes one Zarr chunk, and an array is assembled across
as many instances as the dataset spans.

```python
from minus80.parsers.dicom import multiplex_array

marr, markers = multiplex_array(dcm_paths)      # one marker per instance -> (c, y, x)
marr.metadata.attributes["channel_names"]       # ['DNA', 'Pan Keratin', ...]
```

| Function | Input | Returns |
| --- | --- | --- |
| `multiplex_array(sources, url_for=)` | Single-frame microscopy instances, one optical path (marker) each | A `(c, y, x)` `ManifestArray`, one chunk per channel, ordered by `OpticalPathIdentifier`, and the marker names from `OpticalPathDescription` |
| `volume_array(sources, url_for=)` | Single-slice radiology instances (CT, MR, PET) | A `(z, y, x)` `ManifestArray`, one chunk per slice, ordered by `ImagePositionPatient` z (else `InstanceNumber`), and the z positions and median spacing |
| `wsi_level_array(path, url_for=)` | One tiled whole-slide pyramid level, a multi-frame instance | A `(c, y, x)` (RGB) or `(y, x)` `ManifestArray`, one chunk per tile |
| `pyramid_levels(sources)` | A whole-slide series | Its `VOLUME` instances as `(rows, cols, source)`, largest first; overview, thumbnail and label instances are left out |
| `dicom_json(path)` | Any instance | The full element tree as DICOM JSON (PS3.18), pixel data excluded, binary elements inline as base64 |
| `native_pixeldata_byte_range(path, n)` | An uncompressed instance | The file offset and length of the PixelData value |
| `encapsulated_frame_ranges(path)` | A compressed instance | The file offset and length of each encoded frame |

`url_for` maps a local source to the URL written into the manifest. It defaults
to the file's own `file://` URL; pass a function to parse a local copy while
referencing the same bytes on a remote store.

## What comes from where

- **pydicom** reads every element and leaves the file positioned at PixelData
  (`dcmread(..., stop_before_pixels=True)`), and finds every fragment Item of a
  compressed PixelData (`pydicom.encaps.parse_fragments`). The parser adds only
  the arithmetic from those positions to the value bytes. No DICOM library
  exposes a file offset, because readers decode rather than locate.
- **imagecodecs** decodes compressed tiles at read time, through the Zarr 3
  codecs that `imagecodecs-zarr` registers (`imagecodecs_jpeg2k`,
  `imagecodecs_jpeg`). A store reads with zarr, imagecodecs and
  imagecodecs-zarr, and no DICOM library.
- **The parser** builds the manifests: per-frame byte ranges, the assembly of
  many single-frame instances into one array, and the codec chain.

## The codec chain

An uncompressed frame uses Zarr's `bytes` codec. A compressed tile uses the
image codec as the serializer, since a JPEG decodes straight to an array.

RGB tiles are stored pixel-interleaved, `(y, x, c)`, but OME-Zarr puts the
channel axis before space. The array is declared `(c, y, x)` and a `transpose`
codec with order `(1, 2, 0)` sits first in the chain, so the interleaved bytes
decode channel-first without a byte moving. A pyramid built from the array
then keeps the same axis order at every level.

A frame whose fragment Item holds no bytes becomes a missing chunk, read as the
fill value 0, because Icechunk rejects a zero-length reference.

## Locating the bytes

- **Uncompressed.** After pydicom stops at PixelData, the parser reads that one
  element header (12 bytes for the long explicit-VR forms such as `OB` and `OW`,
  8 otherwise, including implicit VR) and steps to the value. Frames are
  contiguous and fixed-size, so frame *i* starts at
  `value_offset + i * frame_bytes`.
- **Compressed.** PixelData is a Basic Offset Table Item followed by one
  fragment Item per frame. Each fragment's value starts 8 bytes after its Item
  tag. In `TILED_FULL` order frame *i* is tile `(i // n_cols, i % n_cols)`.

Two shortcuts gave wrong offsets; the tests cover the second:

- **PixelData is not always the last element.** Computing the offset as
  `file_size - pixel_bytes` was wrong for real whole-slide microscopy instances,
  which carried about 356 bytes of trailing elements after the pixel value.
- **The PixelData tag's bytes can occur earlier.** Searching the file for
  `E0 7F 10 00` can match inside another element's value, such as an ICC
  profile.

`dicom_json` keeps binary elements however large because some are metadata a
reader needs: a slide's ICC colour profile is a value of about 140 KB inside
`OpticalPathSequence`.

Multiplex channels are ordered by `OpticalPathIdentifier` rather than by name,
because names repeat: a slide can carry two channels labelled `DNA`, stained in
different cycles.

## Limits

- **Transfer syntaxes:** uncompressed (Explicit or Implicit VR Little Endian)
  everywhere; JPEG Baseline, JPEG Extended and JPEG 2000 for whole-slide levels
  only. Anything else raises `ValueError`, including JPEG-XR, which has no Zarr
  codec.
- **Multiplex** reads one instance per optical path. A series that stores every
  channel in one multi-frame instance is not supported.
- **Volumes** are single-frame slices. Enhanced multi-frame CT and MR, with the
  whole volume in one instance, are not supported.
- **Whole-slide levels** must be one `TILED_FULL` instance with one fragment
  per frame. `TILED_SPARSE` and levels concatenated across instances are not
  supported.
- **Pixel spacing is not read.** `PixelSpacing` and the whole-slide spacing in
  the functional groups stay in `dicom_json`; only a volume's z spacing is
  computed.
- **Lossy frames are not byte-identical to every reader.** Two conformant JPEG
  2000 decoders can differ by one level on an irreversible codestream, so
  compare a read against imagecodecs' decode of the same fragment, not against
  `pixel_array` from a reader that used another decoder.

## Upstream

- **VirtualiZarr has no DICOM parser.** DICOM's frames at known offsets are the
  same shape of problem as tiled TIFF, which it does parse.
- **wsidicom or highdicom** model `TILED_SPARSE` positions and concatenated
  levels, and are the libraries to buy for those layouts. pydicom suffices for
  `TILED_FULL`.

## Tests

```bash
uv run pytest tests/parsers/dicom
```

The tests write small synthetic instances with `minus80.parsers.dicom.synthetic`,
so they need no network. Each reads the virtual array back through Zarr and
compares it with the pixels written, or with pydicom's own decode.
