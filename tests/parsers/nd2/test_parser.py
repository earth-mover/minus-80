"""Tests for the ND2 parser, against one real ND2 from IDR.

The remote test reads only the index, under a megabyte. The others need the
whole 101 MB file; it is cached in ``MINUS80_ND2_TEST_CACHE`` (default
``.cache/`` beside this file) and downloaded only when
``MINUS80_ND2_TEST_DOWNLOAD`` is set, else those tests skip.
"""

from __future__ import annotations

import io
import os
import pathlib
import shutil
import socket
import urllib.parse
import urllib.request
from types import SimpleNamespace

import numpy as np
import pytest

from minus80.parsers.nd2 import UnsupportedND2, index_nd2
from minus80.parsers.nd2.parser import _describe, _pixel_size_um

#: The smallest canonical ND2 of idr0095: 4 stage positions, 3 channels,
#: 2048 x 2048 uint16, 101 MB, uncompressed.
SMALL = ("https://ftp.ebi.ac.uk/pub/databases/IDR/idr0095-ali-asymmetry/"
         "20200831-ftp/Fig5A_O1/RDMT3/0X.nd2")
PIXEL_UM = 0.064468161573657

CACHE = pathlib.Path(os.environ.get(
    "MINUS80_ND2_TEST_CACHE", pathlib.Path(__file__).parent / ".cache"))


def local_copy() -> pathlib.Path:
    path = CACHE / "idr0095-RDMT3-0X.nd2"
    if path.exists():
        return path
    if not os.environ.get("MINUS80_ND2_TEST_DOWNLOAD"):
        pytest.skip(f"no local ND2 at {path}; set MINUS80_ND2_TEST_DOWNLOAD=1 to fetch 101 MB")
    CACHE.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SMALL, timeout=600) as r, open(path, "wb") as fh:
        shutil.copyfileobj(r, fh)
    return path


def check_index(index) -> None:
    """What every reader of this file should find, whatever it was read over."""
    assert index.positions == 4
    assert index.frame_shape == (3, 2048, 2048)
    assert index.dtype == np.dtype("uint16")
    assert index.frame_bytes == 2048 * 2048 * 3 * 2
    assert len(index.stage_positions_um) == 4
    attrs = index.position_array(1, attributes={"study": "idr0095"}).metadata.attributes
    assert attrs["channel_names"] == ["Phase", "Tx Red", "YFP"]
    assert attrs["scale"] == pytest.approx({"y": PIXEL_UM, "x": PIXEL_UM})
    assert attrs["units"] == {"y": "micrometer", "x": "micrometer"}
    assert attrs["stage_position_um"] == list(index.stage_positions_um[1])
    assert attrs["source_url"] == index.url
    assert attrs["study"] == "idr0095"


def reachable(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname
    try:
        socket.create_connection((host, 443), timeout=10).close()
    except OSError:
        return False
    return True


@pytest.mark.network
def test_index_over_https_is_bounded():
    """Indexing a remote ND2 costs a handful of requests and no pixel bytes."""
    if not reachable(SMALL):
        pytest.skip(f"{urllib.parse.urlsplit(SMALL).hostname} is not answering on 443")
    index = index_nd2(SMALL)
    check_index(index)
    assert index.url == SMALL
    assert index.size == 101048320
    assert index.requests <= 4
    # Anything approaching a frame (25 MB) would mean pixels were fetched.
    assert index.fetched_bytes < 1 << 20


def test_local_path_and_file_object_index_the_same():
    path = local_copy()
    by_path = index_nd2(path)
    check_index(by_path)
    assert (by_path.requests, by_path.fetched_bytes) == (0, 0)
    with open(path, "rb") as fh:
        by_file = index_nd2(fh, url=SMALL)
        assert not fh.closed
    assert by_file.url == SMALL
    assert by_file.frame_offsets == by_path.frame_offsets


def test_file_object_without_url_is_refused():
    with pytest.raises(ValueError, match="url="):
        index_nd2(io.BytesIO(b""))


def test_frame_bytes_identical_to_nd2(tmp_path):
    """Every array read through Icechunk equals what ``nd2`` decodes from the file."""
    import nd2
    from minus80.core.icechunk_io import commit, create_repo, open_repo
    from virtualizarr.manifests import ManifestGroup

    path = local_copy()
    index = index_nd2(path)
    arrays = index.position_arrays()
    assert list(arrays) == [str(p) for p in range(index.positions)]
    stacked = index.stacked_array()
    assert stacked.metadata.chunks == (1, *index.frame_shape)
    arrays["stacked"] = stacked

    repo = create_repo(tmp_path / "repo", [index.url])
    session = repo.writable_session("main")
    ManifestGroup(arrays=arrays).to_icechunk(session.store)
    commit(session, "one ND2")

    with nd2.ND2File(path) as f:
        truth = f.asarray()                      # (position, c, y, x)
    root = open_repo(tmp_path / "repo")
    for p in range(index.positions):
        assert np.array_equal(root[str(p)][:], truth[p])
    assert np.array_equal(root["stacked"][:], truth)


def _source(compression=None, width_bytes=12288, loops=("XYPosLoop",)):
    attributes = SimpleNamespace(compressionType=compression, componentCount=3,
                                 heightPx=2048, widthPx=2048, widthBytes=width_bytes)
    return SimpleNamespace(attributes=attributes, dtype="uint16",
                           experiment=[SimpleNamespace(type=k) for k in loops])


@pytest.mark.parametrize("source, match", [
    (_source(compression="lossless"), "compressed"),
    (_source(width_bytes=16384), "padded rows"),
    (_source(loops=("XYPosLoop", "TimeLoop")), "TimeLoop"),
])
def test_refuses_frames_that_are_not_plain_chunks(source, match):
    with pytest.raises(UnsupportedND2, match=match):
        _describe(source, url=SMALL, size=0, requests=0, fetched_bytes=0)


def test_uncalibrated_axes_have_no_pixel_size():
    volume = SimpleNamespace(axesCalibration=(1.0, 0.5, 1.0), axesCalibrated=(False, True, False))
    assert _pixel_size_um([SimpleNamespace(volume=volume)]) == {"y": 0.5}
    assert _pixel_size_um([]) == {}
