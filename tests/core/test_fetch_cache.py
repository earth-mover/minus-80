"""Two sources with one filename must not share a cache slot.

An archive names files by their position in a directory tree, so the same
basename recurs constantly: idr0095 has 412 source files and 19 distinct
basenames, and `1X.nd2` alone names 54 different files. A cache keyed on the
basename hands the first file's bytes to all 54, with no error anywhere -- the
pixels are simply wrong, and every gate that compares a store against "the
source" compares it against the wrong one.
"""

from __future__ import annotations

import numpy as np
import pytest

from minus80.core.fetch import download_then_index
from minus80.core.registry import as_url


@pytest.fixture
def two_files_one_name(tmp_path):
    """Two files called ``1X.nd2``, in different directories, holding different bytes."""
    made = []
    for i, folder in enumerate(("Trial1", "Trial2")):
        directory = tmp_path / "source" / folder
        directory.mkdir(parents=True)
        path = directory / "1X.nd2"
        path.write_bytes(bytes([i]) * 4096)
        made.append(path)
    return made


def test_a_shared_basename_does_not_share_a_cache_slot(two_files_one_name, tmp_path):
    first, second = two_files_one_name
    cache = tmp_path / "cache"

    one = download_then_index(as_url(str(first)), str(cache))
    two = download_then_index(as_url(str(second)), str(cache))

    assert one != two, "two different sources resolved to one cached path"
    assert np.frombuffer(open(one, "rb").read(), dtype="u1")[0] == 0
    assert np.frombuffer(open(two, "rb").read(), dtype="u1")[0] == 1


def test_the_same_source_twice_is_one_download(two_files_one_name, tmp_path):
    """The cache still caches: asking twice returns the same path, not a copy."""
    first, _ = two_files_one_name
    cache = tmp_path / "cache"
    url = as_url(str(first))
    assert download_then_index(url, str(cache)) == download_then_index(url, str(cache))


def test_the_cached_name_keeps_its_extension(two_files_one_name, tmp_path):
    """Parsers sniff the extension, so the slot goes above the file, not into its name."""
    first, _ = two_files_one_name
    got = download_then_index(as_url(str(first)), str(tmp_path / "cache"))
    assert got.endswith("1X.nd2")
