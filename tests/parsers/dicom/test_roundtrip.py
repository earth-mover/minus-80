"""Synthetic DICOM instances, virtualized and read back through Zarr, compared with
the pixels written or with pydicom's decode of the same file."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pydicom")

from minus80.parsers.dicom import (  # noqa: E402
    dicom_json,
    encapsulated_frame_ranges,
    multiplex_array,
    native_pixeldata_byte_range,
    volume_array,
    wsi_level_array,
    wsi_levels,
)
from minus80.parsers.dicom.synthetic import (  # noqa: E402
    write_encapsulated_level,
    write_multiplex_series,
    write_volume_series,
    write_wsi_pyramid_series,
)

MARKERS = ["DNA", "Pan Keratin", "CD8a", "SMA", "CD31"]
MM = {"y": "millimeter", "x": "millimeter"}


def _read(marr) -> np.ndarray:
    """Decode a ManifestArray through Zarr, the path a reader of the store takes."""
    import zarr
    from virtualizarr.manifests import ManifestGroup, ManifestStore

    from minus80.core import registry_for

    url = next(marr.manifest.iter_nonempty_paths())
    store = ManifestStore(ManifestGroup(arrays={"image": marr}), registry=registry_for(url))
    return zarr.open(store, mode="r")["image"][:]


def _jpeg2k_level(path, frames, offset_table="none", **kw):
    from pydicom.uid import JPEG2000Lossless

    return write_encapsulated_level(path, frames, tile=(4, 4), columns=4 * len(frames),
                                    transfer_syntax=JPEG2000Lossless,
                                    offset_table=offset_table, **kw)


def test_multiplex_orders_channels_by_optical_path_and_states_spacing(tmp_path):
    paths, expected = write_multiplex_series(tmp_path, MARKERS, spacing=(0.0005, 0.0004))
    marr = multiplex_array(list(reversed(paths)))

    attrs = marr.metadata.attributes
    assert attrs["channel_names"] == MARKERS
    assert attrs["scale"] == {"y": 0.0005, "x": 0.0004}
    assert attrs["units"] == MM
    assert marr.metadata.dimension_names == ("c", "y", "x")
    np.testing.assert_array_equal(_read(marr), expected)


def test_native_byte_range_decodes_to_pydicom_pixel_array(tmp_path):
    import pydicom

    paths, _ = write_multiplex_series(tmp_path, MARKERS[:1], shape=(24, 32))
    off, length = native_pixeldata_byte_range(paths[0], 24 * 32 * 2)
    raw = Path(paths[0]).read_bytes()[off : off + length]
    np.testing.assert_array_equal(np.frombuffer(raw, "<u2").reshape(24, 32),
                                  pydicom.dcmread(paths[0]).pixel_array)


def test_volume_orders_slices_by_position_and_states_spacing(tmp_path):
    z = [-10.0, -7.5, -5.0, -2.5]
    paths, expected = write_volume_series(tmp_path, z, spacing=(0.7, 0.6))
    marr = volume_array(paths)

    attrs = marr.metadata.attributes
    assert attrs["z_positions"] == z
    assert attrs["scale"] == {"z": 2.5, "y": 0.7, "x": 0.6}
    assert attrs["units"] == {"z": "millimeter", **MM}
    image = _read(marr)
    assert image.dtype == np.int16
    np.testing.assert_array_equal(image, expected)


def test_fewer_bits_stored_than_allocated_is_refused(tmp_path):
    import pydicom

    paths, _ = write_volume_series(tmp_path, [0.0, 1.0])
    for path in paths:
        ds = pydicom.dcmread(path)
        ds.BitsStored, ds.HighBit = 12, 11
        ds.save_as(path)
    with pytest.raises(ValueError, match="12 of 16 bits stored"):
        volume_array(paths)


def test_volume_without_positions_orders_by_instance_number_and_omits_z_scale(tmp_path):
    paths, expected = write_volume_series(tmp_path, [None, None, None], spacing=None)
    marr = volume_array(paths)

    assert "scale" not in marr.metadata.attributes
    assert "z_positions" not in marr.metadata.attributes
    np.testing.assert_array_equal(_read(marr), expected)


def test_wsi_levels_are_largest_first_with_their_own_spacing(tmp_path):
    sizes = [(512, 640), (256, 320), (128, 160)]
    paths, expected = write_wsi_pyramid_series(tmp_path, sizes, tile=256, spacing=(0.00025, 0.00025))
    levels = wsi_levels([paths[1], paths[2], paths[0]])

    assert list(levels) == ["0", "1", "2"]
    for i, ((y, x), exp) in enumerate(zip(sizes, expected)):
        level = levels[str(i)]
        assert level.metadata.dimension_names == ("c", "y", "x")
        assert level.metadata.attributes["scale"] == pytest.approx(
            {"y": 0.00025 * 512 / y, "x": 0.00025 * 640 / x})
        np.testing.assert_array_equal(np.moveaxis(_read(level), 0, -1), exp)


def test_wsi_levels_refuses_instances_from_several_optical_paths(tmp_path):
    dna, _ = write_wsi_pyramid_series(tmp_path, [(32, 32), (16, 16)], tile=16, samples=1,
                                      optical_path="1")
    cd8, _ = write_wsi_pyramid_series(tmp_path, [(32, 32), (16, 16)], tile=16, samples=1,
                                      optical_path="2")
    with pytest.raises(ValueError, match="optical paths"):
        wsi_levels(dna + cd8)


def test_signed_wsi_level_reads_signed(tmp_path):
    import pydicom

    paths, expected = write_wsi_pyramid_series(tmp_path, [(40, 40)], tile=16, samples=1,
                                               dtype=np.int16)
    image = _read(wsi_level_array(paths[0]))
    assert image.dtype == np.int16
    assert image.min() < 0
    np.testing.assert_array_equal(image, expected[0])
    tiles = pydicom.dcmread(paths[0]).pixel_array
    np.testing.assert_array_equal(image[16:32, 16:32], tiles[4])


@pytest.mark.parametrize("table", ["none", "basic", "extended"])
def test_frame_ranges_from_each_offset_table_locate_every_frame(tmp_path, table):
    frames = [b"frame-0", b"frame-one", b"frame-two!", b"f3"]
    path = _jpeg2k_level(tmp_path / "level.dcm", frames, table)
    raw = Path(path).read_bytes()
    padded = [f + b"\x00" * (len(f) % 2) for f in frames]
    assert [raw[o : o + n] for o, n in encapsulated_frame_ranges(path)] == padded


def test_frame_split_into_fragments_is_refused(tmp_path):
    import pydicom
    from pydicom.encaps import encapsulate

    path = _jpeg2k_level(tmp_path / "level.dcm", [b"abcdefgh", b"ijklmnop"], "basic")
    ds = pydicom.dcmread(path)
    ds.PixelData = encapsulate([b"abcdefgh", b"ijklmnop"], fragments_per_frame=2, has_bot=True)
    ds.save_as(path)
    with pytest.raises(ValueError, match="not one fragment"):
        encapsulated_frame_ranges(path)


def test_frame_ranges_ignore_pixeldata_tag_bytes_in_the_header(tmp_path):
    """The PixelData tag's bytes inside an earlier value (here an ICC profile) are not mistaken for it."""
    frames = [b"frame-zero", b"frame-one!", b"frame-two."]
    path = _jpeg2k_level(tmp_path / "decoy.dcm", frames, icc=b"\xe0\x7f\x10\x00" * 8)
    raw = Path(path).read_bytes()
    assert raw.find(b"\xe0\x7f\x10\x00") < raw.rfind(b"\xe0\x7f\x10\x00")
    assert [raw[o : o + n] for o, n in encapsulated_frame_ranges(path)] == frames


def test_empty_encapsulated_frame_is_a_missing_chunk(tmp_path):
    path = _jpeg2k_level(tmp_path / "sparse.dcm", [b"\x00" * 8, b"", b"\x00" * 8])
    marr = wsi_level_array(path)
    assert set(marr.manifest.dict()) == {"0.0", "0.2"}


def test_remote_level_matches_local_and_downloads_only_without_a_table(
        tmp_path, range_server, monkeypatch):
    monkeypatch.setenv("MINUS80_CACHE_DIR", str(tmp_path / "cache"))
    frames = [bytes([i]) * 64 for i in range(1, 9)]
    for table in ("extended", "basic", "none"):
        path = _jpeg2k_level(tmp_path / f"{table}.dcm", frames, table)
        range_server.files[f"/{table}.dcm"] = Path(path).read_bytes()
        url = f"{range_server.url}/{table}.dcm"
        local = wsi_level_array(path, url=url)
        remote = wsi_level_array(url)
        assert remote.manifest.dict() == local.manifest.dict()
    downloaded = {p.name for p in (tmp_path / "cache").rglob("*.dcm")}
    assert downloaded == {"none.dcm"}


def test_file_object_source_records_the_given_url(tmp_path):
    paths, _ = write_wsi_pyramid_series(tmp_path, [(32, 32)], tile=16)
    with open(paths[0], "rb") as f:
        with pytest.raises(ValueError, match="url="):
            wsi_level_array(f)
        marr = wsi_level_array(f, url="s3://bucket/level_0.dcm")
    assert set(marr.manifest.iter_nonempty_paths()) == {"s3://bucket/level_0.dcm"}
    assert marr.manifest.dict() == wsi_level_array(
        paths[0], url="s3://bucket/level_0.dcm").manifest.dict()


def test_compressed_tiles_decode_through_imagecodecs_zarr(tmp_path):
    import imagecodecs
    from pydicom.uid import JPEG2000Lossless, JPEGBaseline8Bit

    tiles = np.random.default_rng(1).integers(0, 256, size=(2, 16, 16, 3), dtype=np.uint8)
    for xfer, encode, decode, photometric in [
        (JPEG2000Lossless, lambda t: imagecodecs.jpeg2k_encode(t, level=0),
         imagecodecs.jpeg2k_decode, "RGB"),
        (JPEGBaseline8Bit, lambda t: imagecodecs.jpeg8_encode(t, level=90),
         imagecodecs.jpeg8_decode, "YBR_FULL_422"),
    ]:
        frames = [encode(t) for t in tiles]
        path = write_encapsulated_level(tmp_path / f"{xfer.keyword}.dcm", frames, tile=(16, 16),
                                        columns=32, transfer_syntax=xfer, samples=3,
                                        photometric=photometric)
        image = _read(wsi_level_array(path))
        expected = np.concatenate([decode(f) for f in frames], axis=1).transpose(2, 0, 1)
        np.testing.assert_array_equal(image, expected)
    np.testing.assert_array_equal(  # the lossless pass equals the pixels encoded
        _read(wsi_level_array(tmp_path / f"{JPEG2000Lossless.keyword}.dcm")),
        np.concatenate(list(tiles), axis=1).transpose(2, 0, 1))


def test_dicom_json_keeps_bulk_elements_inline(tmp_path):
    """A slide's ICC profile is metadata a reader needs, so it survives as base64."""
    import base64

    icc = bytes(range(256)) * 20
    path = _jpeg2k_level(tmp_path / "icc.dcm", [b"\x01" * 8], icc=icc)
    with open(path, "rb") as f:
        optical_path = dicom_json(io.BytesIO(f.read()))["00480105"]["Value"][0]
    assert base64.b64decode(optical_path["00282000"]["InlineBinary"]) == icc
