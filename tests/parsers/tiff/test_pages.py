"""Pages written by tifffile, referenced, read back through Icechunk, compared with tifffile's decode."""

import numpy as np
import pytest
import tifffile
import zarr
from virtualizarr.manifests import ManifestGroup

from minus80.core.icechunk_io import commit, create_repo, open_repo
from minus80.parsers.tiff import page_array, page_codecs, resolution_um, series_arrays


def _picture(shape, seed=0):
    """Smooth content, so JPEG has something to compress that is not noise."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[: shape[0], : shape[1]]
    base = (np.sin(y / 17) + np.cos(x / 23)) * 60 + 128
    planes = [base + rng.normal(0, 4, base.shape) + 20 * k for k in range(shape[2] if len(shape) == 3 else 1)]
    return np.clip(np.stack(planes, -1).squeeze(), 0, 255).astype(np.uint8)


def _roundtrip(tmp_path, path, arrays):
    repo_path = tmp_path / "repo"
    repo = create_repo(repo_path, [path.as_uri()])
    session = repo.writable_session("main")
    ManifestGroup(arrays=arrays).to_icechunk(session.store)
    commit(session, "test")
    return open_repo(repo_path)


@pytest.mark.parametrize("photometric", ["rgb", "ycbcr"])
def test_jpeg_tiles_match_tifffile(tmp_path, photometric):
    data = _picture((300, 520, 3))
    path = tmp_path / "rgb.tif"
    tifffile.imwrite(path, data, tile=(128, 128), compression="jpeg", photometric=photometric)
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        want = page.asarray()
        arr = page_array(page, path.as_uri())
        assert page_codecs(page)[0]["name"] == "transpose"
    assert arr.shape == (3, 300, 520) and arr.metadata.dimension_names == ("c", "y", "x")
    got = _roundtrip(tmp_path, path, {"0": arr})["0"][:]
    np.testing.assert_array_equal(got, np.moveaxis(want, -1, 0))


def test_lzw_pyramid_matches_tifffile(tmp_path):
    data = _picture((700, 900))
    path = tmp_path / "grey.tif"
    with tifffile.TiffWriter(path) as tw:
        tw.write(data, tile=(256, 256), compression="lzw", subifds=1)
        tw.write(data[::2, ::2], tile=(256, 256), compression="lzw", subfiletype=1)
    with tifffile.TiffFile(path) as tf:
        arrays = series_arrays(tf.series[0], path.as_uri())
        want = [level.asarray() for level in tf.series[0].levels]
    root = _roundtrip(tmp_path, path, arrays)
    for i, w in enumerate(want):
        assert root[str(i)].metadata.dimension_names == ("y", "x")
        np.testing.assert_array_equal(root[str(i)][:], w)


def test_single_strip_is_one_chunk(tmp_path):
    data = _picture((90, 140, 3))
    path = tmp_path / "strip.tif"
    tifffile.imwrite(path, data, compression="jpeg", photometric="rgb", rowsperstrip=90)
    with tifffile.TiffFile(path) as tf:
        arr = page_array(tf.pages[0], path.as_uri())
        want = tf.pages[0].asarray()
    assert arr.metadata.chunks == (3, 90, 140)
    got = _roundtrip(tmp_path, path, {"0": arr})["0"][:]
    np.testing.assert_array_equal(got, np.moveaxis(want, -1, 0))


def test_short_final_strip_refused(tmp_path):
    path = tmp_path / "strips.tif"
    tifffile.imwrite(path, _picture((100, 64)), compression="lzw", rowsperstrip=30)
    with tifffile.TiffFile(path) as tf, pytest.raises(NotImplementedError, match="short final strip"):
        page_array(tf.pages[0], path.as_uri())


def test_resolution_needs_every_tag(tmp_path):
    stated, bare = tmp_path / "stated.tif", tmp_path / "bare.tif"
    tifffile.imwrite(stated, _picture((16, 16)), resolution=(4.0, 2.0), resolutionunit="MICROMETER")
    tifffile.imwrite(bare, _picture((16, 16)), resolution=None)
    with tifffile.TiffFile(stated) as tf:
        assert resolution_um(tf.pages[0]) == pytest.approx((0.5, 0.25))
    with tifffile.TiffFile(bare) as tf:
        assert resolution_um(tf.pages[0]) is None
