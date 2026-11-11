import numpy as np
import tifffile
from minus80.core.remote import ReadCounter
from measure import check_mask, check_plane, measure_mask, measure_plane, sampled
from minus80.core.icechunk_io import create_repo
from numcodecs import Blosc

from tests.stores import zip_v2_store


def _repo(tmp_path):
    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("measure", repo.lookup_branch("main"))
    return repo


def test_sampled_spreads_over_the_items_and_keeps_both_ends():
    assert sampled(list(range(10)), 4) == [0, 3, 6, 9]
    assert sampled([1, 2], 4) == [1, 2]


def test_measure_plane_writes_each_tile_once_and_reads_no_chunk_back(tmp_path):
    data = np.arange(40 * 50, dtype=np.uint16).reshape(40, 50)
    tifffile.imwrite(tmp_path / "plane.tif", data, tile=(16, 16), compression="zlib")
    repo = _repo(tmp_path)

    with tifffile.TiffFile(tmp_path / "plane.tif") as tif:
        page = tif.pages.first
        result = measure_plane(repo, "measure", "measure/run", page, ReadCounter())
        plane_errors = check_plane(repo, result, page)

    # Tiles match chunks, so the edge tiles fill their chunks too and zarr never reads one back.
    assert (result["tiles"], result["store_sets"], result["store_gets"]) == (12, 12, 0)
    assert result["decoded_bytes"] == data.nbytes
    assert plane_errors == []
    assert repo.lookup_branch("measure") == result["snapshot"]


def test_measure_mask_copies_the_chunk_bytes_unchanged(tmp_path):
    mask = np.arange(40 * 50, dtype=np.uint32).reshape(40, 50)
    zf = zip_v2_store(tmp_path, "cells", {"masks/0": (mask, {"chunks": (16, 16), "compressors": Blosc("zstd")})})
    repo = _repo(tmp_path)

    result = measure_mask(repo, "measure", "measure/run", zf, "masks/0", ReadCounter(), 3)

    assert (result["chunks"], result["store_sets"], result["store_gets"]) == (3, 3, 0)
    assert check_mask(repo, result, zf, "masks/0") == []
