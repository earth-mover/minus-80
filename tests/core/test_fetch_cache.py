"""Two sources with one filename must not share a cache slot.

An archive names files by their position in a directory tree, so the same
basename recurs constantly: idr0095 has 412 source files and 19 distinct
basenames, and `1X.nd2` alone names 54 different files. A cache keyed on the
basename hands the first file's bytes to all 54, with no error anywhere -- the
pixels are simply wrong, and every gate that compares a store against "the
source" compares it against the wrong one.
"""

from pathlib import Path

import pytest

from minus80.core.fetch import download_then_index
from minus80.core.registry import as_url


@pytest.fixture
def two_files_one_name(tmp_path):
    paths = []
    for i, folder in enumerate(("Trial1", "Trial2")):
        path = tmp_path / "source" / folder / "1X.nd2"
        path.parent.mkdir(parents=True)
        path.write_bytes(bytes([i]) * 4096)
        paths.append(path)
    return paths


def test_shared_basename_gets_its_own_cache_slot(two_files_one_name, tmp_path):
    first, second = (download_then_index(as_url(str(p)), str(tmp_path / "cache"))
                     for p in two_files_one_name)
    assert Path(first).read_bytes()[0] == 0
    assert Path(second).read_bytes()[0] == 1
    # Parsers sniff the extension, so the cached file keeps its name.
    assert first.endswith("1X.nd2") and second.endswith("1X.nd2")


def test_same_source_twice_is_one_download(two_files_one_name, tmp_path):
    url = as_url(str(two_files_one_name[0]))
    cache = str(tmp_path / "cache")
    assert download_then_index(url, cache) == download_then_index(url, cache)
