"""Acceptance tests for the DICOM parser. Fully offline: synthetic DICOM instances,
virtualized and read back through Zarr, asserted equal to the pixels written -- two IO
paths that share no code.
"""
from __future__ import annotations

import numpy as np

MARKERS = ["DNA", "Pan Keratin", "CD8a", "SMA", "CD31"]


def _read(marr) -> np.ndarray:
    """Decode a ManifestArray through Zarr, the path a reader of the store takes."""
    import zarr
    from virtualizarr.manifests import ManifestGroup, ManifestStore

    from minus80.core import registry_for

    url = next(marr.manifest.iter_nonempty_paths())
    store = ManifestStore(ManifestGroup(arrays={"image": marr}), registry=registry_for(url))
    return zarr.open(store, mode="r")["image"][:]


def test_multiplex_roundtrip_and_channel_names(tmp_path):
    """N single-frame instances -> one (c, y, x) array that reads back equal to the
    source pixels, with the marker names carried in channel order."""
    from minus80.parsers.dicom import multiplex_array
    from minus80.parsers.dicom.synthetic import write_multiplex_series

    paths, expected = write_multiplex_series(tmp_path / "dcm", MARKERS, shape=(24, 32))
    # Channels are ordered by optical-path id, not by the order sources are passed.
    marr, markers = multiplex_array(list(reversed(paths)))

    assert markers == MARKERS
    assert marr.metadata.attributes["channel_names"] == MARKERS
    assert marr.metadata.dimension_names == ("c", "y", "x")
    image = _read(marr)
    assert image.dtype == expected.dtype
    np.testing.assert_array_equal(image, expected)
    np.testing.assert_array_equal(image[markers.index("DNA")], expected[MARKERS.index("DNA")])


def test_multiplex_chunks_reference_source_pixeldata(tmp_path):
    """Each channel is one chunk referencing its own file's PixelData value in place."""
    from minus80.core import as_url
    from minus80.parsers.dicom import multiplex_array, native_pixeldata_byte_range
    from minus80.parsers.dicom.synthetic import write_multiplex_series

    paths, _ = write_multiplex_series(tmp_path / "dcm", MARKERS, shape=(64, 64))
    marr, _ = multiplex_array(paths)

    assert marr.metadata.chunks == (1, 64, 64)
    manifest = marr.manifest.dict()
    assert len(manifest) == len(MARKERS)
    for c, path in enumerate(paths):
        entry = manifest[f"{c}.0.0"]
        offset, length = native_pixeldata_byte_range(path, 64 * 64 * 2)
        assert (entry["path"], entry["offset"], entry["length"]) == (as_url(path), offset, length)


def test_wsi_pyramid_roundtrip(tmp_path):
    """A multi-level tiled WSI series: the levels come back largest first, and each
    reads back as a channel-first (c, y, x) array equal to the assembled tiles."""
    from minus80.parsers.dicom import pyramid_levels, wsi_level_array
    from minus80.parsers.dicom.synthetic import write_wsi_pyramid_series

    sizes = [(512, 640), (256, 320), (128, 160)]
    paths, expected = write_wsi_pyramid_series(tmp_path / "dcm", sizes, tile=256)

    levels = pyramid_levels([paths[1], paths[2], paths[0]])
    assert [(y, x) for y, x, _ in levels] == sizes

    for (_, _, path), exp in zip(levels, expected):
        marr = wsi_level_array(path)
        assert marr.metadata.dimension_names == ("c", "y", "x")
        assert marr.metadata.chunks == (3, 256, 256)
        image = _read(marr)
        assert image.shape == (3, *exp.shape[:2])
        np.testing.assert_array_equal(np.moveaxis(image, 0, -1), exp)


def test_byte_range_matches_pydicom(tmp_path):
    """The computed PixelData byte range decodes to pydicom's own pixel_array."""
    import pydicom

    from minus80.parsers.dicom import native_pixeldata_byte_range
    from minus80.parsers.dicom.synthetic import write_multiplex_series

    paths, expected = write_multiplex_series(tmp_path / "dcm", MARKERS[:1], shape=(24, 32))
    off, length = native_pixeldata_byte_range(paths[0], 24 * 32 * 2)
    raw = open(paths[0], "rb").read()[off : off + length]
    from_bytes = np.frombuffer(raw, dtype="<u2").reshape(24, 32)
    np.testing.assert_array_equal(from_bytes, pydicom.dcmread(paths[0]).pixel_array)
    np.testing.assert_array_equal(from_bytes, expected[0])


def test_empty_encapsulated_frame_is_a_missing_chunk(tmp_path):
    """A tile whose fragment Item holds no bytes is a missing chunk that reads as the
    fill value, never a zero-length reference (which Icechunk rejects at write)."""
    import struct

    import pydicom
    import zarr
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import JPEG2000Lossless, generate_uid
    from minus80.core import registry_for
    from virtualizarr.manifests import ManifestGroup, ManifestStore

    from minus80.parsers.dicom import wsi_level_array

    def item(value: bytes) -> bytes:
        return struct.pack("<HHI", 0xFFFE, 0xE000, len(value)) + value

    # pydicom's encapsulate() refuses an empty frame, so the fragments are written
    # directly: an empty Basic Offset Table, four frames with the second empty, and
    # the sequence delimiter.
    frames = [b"\x00" * 8, b"", b"\x00" * 8, b"\x00" * 8]
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.6"
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = JPEG2000Lossless
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID = fm.MediaStorageSOPClassUID
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.ImageType = ["DERIVED", "PRIMARY", "VOLUME", "NONE"]
    ds.Rows, ds.Columns = 4, 4
    ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = 8, 8
    ds.NumberOfFrames = len(frames)
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
    ds.PixelRepresentation = 0
    ds.PixelData = (
        item(b"") + b"".join(item(f) for f in frames) + struct.pack("<HHI", 0xFFFE, 0xE0DD, 0)
    )
    ds["PixelData"].VR = "OB"
    ds["PixelData"].is_undefined_length = True
    path = tmp_path / "sparse.dcm"
    pydicom.dcmwrite(path, ds, enforce_file_format=True)

    marr = wsi_level_array(path)
    assert set(marr.manifest.dict()) == {"0.0", "1.0", "1.1"}

    url = next(marr.manifest.iter_nonempty_paths())
    store = ManifestStore(ManifestGroup(arrays={"image": marr}), registry=registry_for(url))
    tile = zarr.open(store, mode="r")["image"][0:4, 4:8]
    np.testing.assert_array_equal(tile, np.zeros((4, 4), dtype=np.uint8))


def _encapsulated_instance(path, frames, *, icc: bytes):
    """A one-sample JPEG 2000 WSI instance whose frames are ``frames`` verbatim,
    with an ICC profile in its OpticalPathSequence."""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.encaps import encapsulate
    from pydicom.uid import JPEG2000Lossless, generate_uid

    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.6"
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = JPEG2000Lossless
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID = fm.MediaStorageSOPClassUID
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.ImageType = ["DERIVED", "PRIMARY", "VOLUME", "NONE"]
    optical_path = Dataset()
    optical_path.OpticalPathIdentifier = "1"
    optical_path.ICCProfile = icc
    ds.OpticalPathSequence = [optical_path]
    ds.Rows, ds.Columns = 4, 4
    ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = 4, 4 * len(frames)
    ds.NumberOfFrames = len(frames)
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
    ds.PixelRepresentation = 0
    ds.PixelData = encapsulate(frames)
    ds["PixelData"].VR = "OB"
    ds["PixelData"].is_undefined_length = True
    pydicom.dcmwrite(path, ds, enforce_file_format=True)
    return path


def test_dicom_json_keeps_bulk_elements_inline(tmp_path):
    """A slide's ICC profile is a binary element nested in OpticalPathSequence; it is
    metadata a reader needs, so it survives as base64 rather than a null BulkDataURI."""
    import base64

    from minus80.parsers.dicom import dicom_json

    icc = bytes(range(256)) * 20
    path = _encapsulated_instance(tmp_path / "icc.dcm", [b"\x01" * 8], icc=icc)
    optical_path = dicom_json(path)["00480105"]["Value"][0]
    assert base64.b64decode(optical_path["00282000"]["InlineBinary"]) == icc


def test_frame_ranges_ignore_pixeldata_tag_bytes_in_the_header(tmp_path):
    """The PixelData tag's four bytes can occur inside an earlier element's value; the
    frames are located from where pydicom stops parsing, not by searching for them."""
    from minus80.parsers.dicom import encapsulated_frame_ranges

    frames = [b"frame-zero", b"frame-one!", b"frame-two."]
    decoy = b"\xe0\x7f\x10\x00" * 8
    path = _encapsulated_instance(tmp_path / "decoy.dcm", frames, icc=decoy)
    raw = path.read_bytes()
    assert raw.find(b"\xe0\x7f\x10\x00") < raw.rfind(b"\xe0\x7f\x10\x00")
    ranges = encapsulated_frame_ranges(path)
    assert [raw[off : off + ln] for off, ln in ranges] == frames


def test_compressed_tiles_use_imagecodecs_serializers(tmp_path):
    """JPEG and JPEG 2000 tiles decode through imagecodecs' own Zarr 3 codecs, named as
    imagecodecs-zarr registers them: transpose, then the image codec as the serializer."""
    import imagecodecs
    import pydicom
    import zarr
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.encaps import encapsulate
    from pydicom.uid import JPEG2000Lossless, JPEGBaseline8Bit, generate_uid
    from minus80.core import registry_for
    from virtualizarr.manifests import ManifestGroup, ManifestStore

    from minus80.parsers.dicom import wsi_level_array

    rng = np.random.default_rng(1)
    tiles = rng.integers(0, 256, size=(2, 16, 16, 3), dtype=np.uint8)
    for name, xfer, encode, photometric in [
        ("imagecodecs_jpeg2k", JPEG2000Lossless, lambda t: imagecodecs.jpeg2k_encode(t, level=0), "RGB"),
        ("imagecodecs_jpeg", JPEGBaseline8Bit, lambda t: imagecodecs.jpeg8_encode(t, level=90), "YBR_FULL_422"),
    ]:
        frames = [encode(t) for t in tiles]
        fm = FileMetaDataset()
        fm.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.6"
        fm.MediaStorageSOPInstanceUID = generate_uid()
        fm.TransferSyntaxUID = xfer
        ds = Dataset()
        ds.file_meta = fm
        ds.SOPClassUID = fm.MediaStorageSOPClassUID
        ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
        ds.ImageType = ["DERIVED", "PRIMARY", "VOLUME", "NONE"]
        ds.Rows, ds.Columns = 16, 16
        ds.TotalPixelMatrixRows, ds.TotalPixelMatrixColumns = 16, 32
        ds.NumberOfFrames = 2
        ds.SamplesPerPixel = 3
        ds.PlanarConfiguration = 0
        ds.PhotometricInterpretation = photometric
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
        ds.PixelRepresentation = 0
        ds.DimensionOrganizationType = "TILED_FULL"
        ds.PixelData = encapsulate(frames)
        ds["PixelData"].VR = "OB"
        ds["PixelData"].is_undefined_length = True
        path = tmp_path / f"{name}.dcm"
        pydicom.dcmwrite(path, ds, enforce_file_format=True)

        marr = wsi_level_array(path)
        codecs = [c.to_dict() for c in marr.metadata.codecs]
        assert codecs == [{"name": "transpose", "configuration": {"order": (1, 2, 0)}}, {"name": name}]
        url = next(marr.manifest.iter_nonempty_paths())
        store = ManifestStore(ManifestGroup(arrays={"image": marr}), registry=registry_for(url))
        image = zarr.open(store, mode="r")["image"][:]
        decode = imagecodecs.jpeg2k_decode if name.endswith("2k") else imagecodecs.jpeg8_decode
        expected = np.concatenate([decode(f) for f in frames], axis=1).transpose(2, 0, 1)
        np.testing.assert_array_equal(image, expected)
        if name.endswith("2k"):  # lossless: equal to the pixels encoded
            np.testing.assert_array_equal(image, np.concatenate(list(tiles), axis=1).transpose(2, 0, 1))


def test_volume_orders_slices_by_position_and_reads_back(tmp_path):
    """Slices written out of order stack by ImagePositionPatient z, signed values intact."""
    from minus80.parsers.dicom.synthetic import write_volume_series
    from minus80.parsers.dicom.volume import volume_array

    z = [-10.0, -7.5, -5.0, -2.5]
    paths, expected = write_volume_series(tmp_path / "ct", z)
    marr, geometry = volume_array(paths)

    assert geometry["z_positions"] == z
    assert geometry["z_spacing"] == 2.5
    assert marr.metadata.dimension_names == ("z", "y", "x")
    image = _read(marr)
    assert image.dtype == np.int16
    np.testing.assert_array_equal(image, expected)


def test_volume_without_positions_falls_back_to_instance_number(tmp_path):
    from minus80.parsers.dicom.synthetic import write_volume_series
    from minus80.parsers.dicom.volume import volume_array

    paths, expected = write_volume_series(tmp_path / "ct", [None, None, None])
    marr, geometry = volume_array(paths)

    assert geometry["z_spacing"] is None
    np.testing.assert_array_equal(_read(marr), expected)
