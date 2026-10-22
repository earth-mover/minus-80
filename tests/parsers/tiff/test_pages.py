"""Levels of TIFFs written by tifffile, read back through Icechunk, compared with tifffile's decode."""

import numpy as np
import pytest
import tifffile
from virtualizarr.manifests import ManifestGroup

from minus80.core.icechunk_io import commit, create_repo, open_repo
from minus80.parsers.tiff import tiff_levels


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
def test_jpeg_rgb_tiles_read_back_channel_first(tmp_path, photometric):
    path = tmp_path / "rgb.tif"
    tifffile.imwrite(path, _picture((300, 520, 3)), tile=(128, 128), compression="jpeg",
                     photometric=photometric)
    arr = tiff_levels(path)["0"]
    assert arr.metadata.dimension_names == ("c", "y", "x")
    assert arr.metadata.attributes["channel_names"] == ["R", "G", "B"]
    got = _roundtrip(tmp_path, path, {"0": arr})["0"][:]
    np.testing.assert_array_equal(got, np.moveaxis(tifffile.imread(path), -1, 0))


def test_lzw_pyramid_levels_read_back(tmp_path):
    data = _picture((700, 900))
    path = tmp_path / "grey.tif"
    with tifffile.TiffWriter(path) as tw:
        tw.write(data, tile=(256, 256), compression="lzw", subifds=1)
        tw.write(data[::2, ::2], tile=(256, 256), compression="lzw", subfiletype=1)
    root = _roundtrip(tmp_path, path, tiff_levels(path))
    with tifffile.TiffFile(path) as tf:
        want = [level.asarray() for level in tf.series[0].levels]
    assert len(want) == 2
    for i, w in enumerate(want):
        assert root[str(i)].metadata.dimension_names == ("y", "x")
        np.testing.assert_array_equal(root[str(i)][:], w)


def test_multipage_ome_series_stacks_pages_on_leading_axes(tmp_path):
    data = np.stack([np.stack([_picture((64, 80), seed=4 * c + z) for z in range(3)]) for c in range(2)])
    path = tmp_path / "stack.ome.tif"
    tifffile.imwrite(path, data, ome=True, tile=(32, 32), compression="lzw",
                     metadata={"axes": "CZYX"})
    arr = tiff_levels(path)["0"]
    assert arr.shape == (2, 3, 64, 80) and arr.metadata.chunks == (1, 1, 32, 32)
    assert arr.metadata.dimension_names == ("c", "z", "y", "x")
    np.testing.assert_array_equal(_roundtrip(tmp_path, path, {"0": arr})["0"][:], data)


def test_empty_tiles_are_missing_chunks(tmp_path):
    path = tmp_path / "partial.tif"
    tiles = [np.full((32, 32), 5, np.uint16), None, np.full((32, 32), 7, np.uint16), None]
    with tifffile.TiffWriter(path) as tw:
        tw.write(iter(tiles), shape=(64, 64), dtype=np.uint16, tile=(32, 32), compression="lzw")
    arr = tiff_levels(path)["0"]
    assert sorted(arr.manifest.dict()) == ["0.0", "1.0"]
    np.testing.assert_array_equal(_roundtrip(tmp_path, path, {"0": arr})["0"][:], tifffile.imread(path))


def test_single_strip_is_one_chunk(tmp_path):
    path = tmp_path / "strip.tif"
    tifffile.imwrite(path, _picture((90, 140, 3)), compression="jpeg", photometric="rgb", rowsperstrip=90)
    arr = tiff_levels(path)["0"]
    assert arr.metadata.chunks == (3, 90, 140)
    got = _roundtrip(tmp_path, path, {"0": arr})["0"][:]
    np.testing.assert_array_equal(got, np.moveaxis(tifffile.imread(path), -1, 0))


def test_short_final_strip_refused(tmp_path):
    path = tmp_path / "strips.tif"
    tifffile.imwrite(path, _picture((100, 64)), compression="lzw", rowsperstrip=30)
    with pytest.raises(NotImplementedError, match="short final strip"):
        tiff_levels(path)


def test_scale_only_from_a_stated_resolution_unit(tmp_path):
    stated, unitless = tmp_path / "stated.tif", tmp_path / "unitless.tif"
    tifffile.imwrite(stated, _picture((16, 16)), resolution=(4.0, 2.0), resolutionunit="MICROMETER")
    tifffile.imwrite(unitless, _picture((16, 16)), resolution=(4.0, 2.0), resolutionunit="NONE")
    attrs = tiff_levels(stated)["0"].metadata.attributes
    assert attrs["scale"] == pytest.approx({"y": 0.5, "x": 0.25})
    assert attrs["units"] == {"y": "micrometer", "x": "micrometer"}
    assert "scale" not in tiff_levels(unitless)["0"].metadata.attributes
