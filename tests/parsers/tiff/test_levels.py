"""Where tiff_levels reads a file from, and what URL the manifest records."""

import numpy as np
import pytest
import tifffile

from minus80.parsers.tiff import levels, tiff_levels
from minus80.parsers.tiff.inspect import DirectoryLayout


@pytest.fixture
def stack(tmp_path):
    """A BigTIFF of 100 small pages, whose directories sit at an even stride."""
    path = tmp_path / "stack.tif"
    tifffile.imwrite(path, np.arange(1600, dtype=np.uint8).reshape(100, 4, 4), bigtiff=True,
                     photometric="minisblack", metadata=None)
    return path


def test_file_object_records_the_url_given(stack):
    with open(stack, "rb") as f, pytest.raises(ValueError, match="url="):
        tiff_levels(f)
    with open(stack, "rb") as f:
        arr = tiff_levels(f, url="s3://bucket/stack.tif")["0"]
    assert arr.metadata.attributes["source_url"] == "s3://bucket/stack.tif"


@pytest.mark.parametrize("prefetch", ["ranges", "directories", "whole"])
def test_remote_manifest_matches_local_parse(stack, range_server, monkeypatch, tmp_path, prefetch):
    monkeypatch.setenv("MINUS80_CACHE_DIR", str(tmp_path / "cache"))
    range_server.files["/stack.tif"] = stack.read_bytes()
    url = f"{range_server.url}/stack.tif"
    want = tiff_levels(stack, url=url)["0"]
    got = tiff_levels(url, prefetch=prefetch)["0"]
    assert got.shape == (100, 4, 4)
    assert got.manifest.dict() == want.manifest.dict()


def test_directories_prefetch_refuses_tags_outside_the_region(stack, range_server, monkeypatch, tmp_path):
    monkeypatch.setenv("MINUS80_CACHE_DIR", str(tmp_path / "cache"))
    range_server.files["/stack.tif"] = data = stack.read_bytes()
    with tifffile.TiffFile(stack) as tf:
        second = tf.pages[1].offset
    # A region that starts after the first directory leaves page 0's tags unfetched.
    monkeypatch.setattr(levels, "directory_layout", lambda source: DirectoryLayout(
        len(data), second, True, directories=99))
    with pytest.raises(ValueError, match="outside the fetched"):
        tiff_levels(f"{range_server.url}/stack.tif", prefetch="directories")


def test_directories_prefetch_refuses_a_classic_tiff(tmp_path, range_server):
    path = tmp_path / "classic.tif"
    tifffile.imwrite(path, np.zeros((4, 4), np.uint8))
    range_server.files["/classic.tif"] = path.read_bytes()
    with pytest.raises(ValueError, match="not bulk-readable"):
        tiff_levels(f"{range_server.url}/classic.tif", prefetch="directories")
