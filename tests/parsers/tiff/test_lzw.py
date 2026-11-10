"""``lzw_valid_length`` on synthetic streams, and the shortened reference read through the Zarr codec."""

import imagecodecs
import numpy as np
import pytest
import zarr
from imagecodecs.zarr import Lzw

from minus80.parsers.tiff import lzw_valid_length


@pytest.fixture
def image():
    rng = np.random.default_rng(0)
    return rng.integers(0, 65535, (64, 64), dtype="<u2")


@pytest.fixture
def overlong(image):
    """Decodes to two bytes more than the image, as a mis-width end code does."""
    return imagecodecs.lzw_encode(image.tobytes() + b"\x00\x00")


def test_a_clean_strip_keeps_its_full_length(image):
    strip = imagecodecs.lzw_encode(image.tobytes())
    length, decoded = lzw_valid_length(strip, image.nbytes)
    assert length == len(strip)
    assert decoded == image.tobytes()


def test_an_overlong_strip_is_cut_to_a_length_that_decodes_exactly(image, overlong):
    assert len(imagecodecs.lzw_decode(overlong)) == image.nbytes + 2
    length, decoded = lzw_valid_length(overlong, image.nbytes)
    assert 0 < length < len(overlong)
    assert imagecodecs.lzw_decode(overlong[:length]) == image.tobytes()
    assert decoded == image.tobytes()


def test_a_strip_no_cut_can_fix_has_no_length(image):
    strip = imagecodecs.lzw_encode(image.tobytes() + bytes(64))
    length, decoded = lzw_valid_length(strip, image.nbytes)
    assert length is None
    assert decoded == image.tobytes()


def test_the_shortened_reference_reads_through_the_zarr_codec(tmp_path, image, overlong):
    length, _ = lzw_valid_length(overlong, image.nbytes)
    arr = zarr.create_array(
        tmp_path / "lzw.zarr", shape=image.shape, chunks=image.shape, dtype=image.dtype,
        zarr_format=3, serializer={"name": "bytes", "configuration": {"endian": "little"}},
        compressors=[Lzw()], fill_value=0)
    chunk = tmp_path / "lzw.zarr" / "c" / "0" / "0"
    chunk.parent.mkdir(parents=True)

    chunk.write_bytes(overlong)
    with pytest.raises(Exception):
        arr[:]

    chunk.write_bytes(overlong[:length])
    np.testing.assert_array_equal(arr[:], image)
