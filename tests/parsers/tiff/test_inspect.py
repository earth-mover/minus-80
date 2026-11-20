import numpy as np
import tifffile

from minus80.parsers.tiff.inspect import codec_chain, directory_layout, survey


def _write(directory):
    tifffile.imwrite(directory / "big.tif", np.zeros((100, 4, 4), np.uint8), bigtiff=True,
                     photometric="minisblack", metadata=None)
    tifffile.imwrite(directory / "classic.tif", np.zeros((4, 4), np.uint8))


def test_bigtiff_of_equal_pages_is_bulk_readable(tmp_path):
    _write(tmp_path)
    layout = directory_layout((tmp_path / "big.tif").as_uri())
    assert layout.bigtiff and layout.bulk_readable
    assert layout.directories == 100


def test_classic_tiff_is_refused(tmp_path):
    _write(tmp_path)
    layout = directory_layout((tmp_path / "classic.tif").as_uri())
    assert not layout.bulk_readable
    assert "classic" in layout.refused


def test_survey_of_a_local_directory(tmp_path):
    _write(tmp_path)
    rows = {r["name"]: r for r in survey(tmp_path.as_uri() + "/")}
    assert rows["big.tif"]["format"] == "BigTIFF"
    assert rows["classic.tif"]["format"] == "TIFF"
    assert rows["classic.tif"]["bytes"] == (tmp_path / "classic.tif").stat().st_size


def test_codec_chain_reports_the_parser_chain_and_its_refusals(tmp_path):
    lzw, deflate = tmp_path / "lzw.tif", tmp_path / "deflate.tif"
    tifffile.imwrite(lzw, np.zeros((16, 16), np.uint8), compression="lzw")
    tifffile.imwrite(deflate, np.zeros((16, 16), np.uint8), compression="zlib")
    chain = codec_chain(lzw)
    assert chain["codecs"] == ["bytes", "imagecodecs_lzw"] and not chain["portable"]
    refused = codec_chain(deflate)
    assert not refused["supported"] and "compression" in refused["reason"]


def test_region_leaves_out_the_pixels_after_a_leading_directory(tmp_path):
    # tifffile writes the first directory before the pixel data and the rest after it.
    path = tmp_path / "contiguous.tif"
    tifffile.imwrite(path, np.zeros((100, 64, 64), np.uint16), bigtiff=True,
                     photometric="minisblack", metadata=None)
    with tifffile.TiffFile(path) as tf:
        first, second = tf.pages[0].offset, tf.pages[1].offset
        pixels_end = tf.pages[-1].dataoffsets[0] + tf.pages[-1].databytecounts[0]
    layout = directory_layout(path.as_uri())
    assert layout.first_directory == first < pixels_end <= second
    assert layout.region == (second, path.stat().st_size)
    assert layout.directories == 100
