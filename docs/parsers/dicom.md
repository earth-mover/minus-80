# DICOM

How `minus80.parsers.dicom` finds each frame's bytes in a DICOM instance, and
the rules that keep those offsets right. The parser's API and limits are in its
[README](../../src/minus80/parsers/dicom/README.md); rules that apply to every
format are in [formats.md](../formats.md).

DICOM is an archive format: a dataset arrives as many instances, one file each,
and each instance holds one or more frames in its PixelData element. Each frame
is a contiguous run of bytes, so each frame can be one Zarr chunk.

## Locating PixelData

**Locate the pixel value from where the parser stopped.** pydicom's
`dcmread(f, stop_before_pixels=True)` parses every element before PixelData and
leaves `f` at the PixelData tag. The parser reads that one element header and
steps over it: 12 bytes for the long explicit-VR forms (`OB`, `OW` and the
other `O*` VRs, and `UN`), 8 otherwise, including implicit VR. No DICOM library
exposes the offset itself, because readers decode rather than locate.

Two shortcuts give wrong offsets:

- **Computing from the end of the file.** PixelData is not always the last
  element. Real whole-slide microscopy instances have carried about 356 bytes
  of elements after the pixel value.
- **Searching for the tag's bytes.** `E0 7F 10 00` can occur inside an earlier
  element's value, such as an ICC profile.

## Frames

**Uncompressed frames are contiguous and fixed-size**, so frame *i* starts at
`value_offset + i * frame_bytes`.

**A compressed frame is one fragment Item**, whose value starts 8 bytes after
the Item tag. PixelData then holds a Basic Offset Table Item and one Item per
frame. A frame split into several fragments would need its fragments joined
before decoding, which a byte-range reference cannot do, so the parser refuses
it.

**An empty fragment is a missing chunk.** A frame whose Item holds no bytes
reads as the fill value 0, because Icechunk rejects a zero-length reference.

In `TILED_FULL` order, frame *i* is tile `(i // n_cols, i % n_cols)`.

## Offset tables

**Read the index the file carries before walking its structure.** An
encapsulated PixelData can carry two indexes of its frames:

- The **Extended Offset Table** (7FE0,0001) and its lengths (7FE0,0002) are
  header elements, so pydicom reads them with the rest of the header. They give
  every frame's offset and length. Frames that are one fragment each sit end to
  end, so the parser checks that each offset is the previous offset plus its
  length plus 8.
- The **Basic Offset Table** is the first Item of PixelData. It gives offsets
  only; each length is the gap to the next offset less the 8-byte Item header,
  and the last frame's length comes from its own Item header. The table cannot
  show that a frame is one fragment, so the parser checks the first frame's
  Item header against it.

**When neither table exists, download the file once.** Each Item header gives
the next one's position, so a walk over range requests costs a round trip per
frame, about 100,000 for a large slide. One parallel download costs far less.

Whole-slide images on IDC's public bucket (`s3://idc-open-data`) have both
kinds. Indexing each base level from a laptop, read anonymously:

| Base level | Frames | Offset table | Requests | Fetched | Time |
| --- | --- | --- | --- | --- | --- |
| GTEx, 399 MB | 36,504 | Basic | 5 | 0.4 MB | 0.4 s |
| TCGA-BRCA, 461 MB | 100,329 | none | 17 | 461 MB | 11.6 s |
| CPTAC-LUAD, 492 MB | 28,182 | none | 18 | 492 MB | 14.0 s |

Every chunk's byte range in two smaller IDC levels, GTEx JPEG with a Basic
Offset Table and TCGA-LUAD JPEG 2000 without one, matched pydicom's own frame
extraction. Frames read from S3 through Zarr equalled pydicom's `pixel_array`.

## Pixel values

**Take the dtype from pydicom's `pixel_dtype`.** It reads `PixelRepresentation`,
so signed data reads as signed. A hand-written mapping from `BitsAllocated`
alone reads signed data as unsigned.

**Refuse `BitsStored` below `BitsAllocated`.** pydicom masks the unused high
bits of unsigned data and sign-extends signed data into them. No Zarr codec does
either, so a Zarr read returns the high bits as stored. When they hold overlay
data, or a signed value that was not sign-extended, every affected pixel reads
differently from pydicom, with no error.

## Codecs

An uncompressed frame uses Zarr's `bytes` codec. A compressed frame uses
imagecodecs' Zarr 3 codec as the serializer, since a JPEG decodes straight to an
array; the parser takes the codec's configuration from `imagecodecs.zarr`
(`Jpeg2k().to_dict()`, `Jpeg().to_dict()`).

RGB tiles are stored pixel-interleaved, `(y, x, c)`, and OME-Zarr puts the
channel axis first. The array is declared `(c, y, x)` and a `transpose` codec
leads the chain, so the bytes decode channel-first without being rewritten.

## Metadata

**Keep binary elements inline.** `dicom_json` writes every binary element as
base64 however large, because some are metadata a reader needs: a slide's ICC
colour profile is about 140 KB inside `OpticalPathSequence`.

**Order channels by identifier, not name.** Channel names repeat: a slide can
carry two channels labelled `DNA`, stained in different cycles. Channels are
ordered by `OpticalPathIdentifier`.

**Spacing is in millimetres.** DICOM states `PixelSpacing` as (row, column) in
millimetres, so `scale` is `{"y": row, "x": column}` with unit `millimeter`.
Whole-slide levels each state their own spacing in
`SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0]`, so no level's
spacing is derived from another's.

## Upstream

- VirtualiZarr has no DICOM parser. Frames at known offsets are the same
  problem as tiled TIFF, which it does parse.
- wsidicom and highdicom model `TILED_SPARSE` positions and levels
  concatenated across instances. They are the libraries to use for those
  layouts; pydicom is enough for `TILED_FULL`.
