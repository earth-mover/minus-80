"""Tests for the ND2 parser, against one real ND2 from IDR.

The remote test reads only the index -- four range requests, under a megabyte
-- so it is quick over any connection. The byte-identity test needs the whole
file, because ``nd2``'s own reader takes a path; it downloads once into
``MINUS80_ND2_TEST_CACHE`` (default ``.cache/`` beside this file) and reuses it,
and skips rather than downloads unless ``MINUS80_ND2_TEST_DOWNLOAD`` is set.
"""

from __future__ import annotations

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
from minus80.parsers.nd2.parser import _describe

#: The smallest canonical ND2 of idr0095: 4 stage positions, 3 channels,
#: 2048 x 2048 uint16, 101 MB. Uncompressed, like every file of that study.
SMALL = ("https://ftp.ebi.ac.uk/pub/databases/IDR/idr0095-ali-asymmetry/"
         "20200831-ftp/Fig5A_O1/RDMT3/0X.nd2")

CACHE = pathlib.Path(os.environ.get(
    "MINUS80_ND2_TEST_CACHE", pathlib.Path(__file__).parent / ".cache"))


def local_copy() -> pathlib.Path:
    """The whole of :data:`SMALL` on disk, downloading it only if asked to."""
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
    assert index.channel_names == ("Phase", "Tx Red", "YFP")
    assert index.pixel_size_um == pytest.approx({"y": 0.064468161573657,
                                                 "x": 0.064468161573657})
    # One frame is one chunk, exactly as stored: y * x * c * itemsize.
    assert index.frame_bytes == 2048 * 2048 * 3 * 2
    assert len(index.stage_positions_um) == 4
    assert index.codecs == [
        {"name": "transpose", "configuration": {"order": [1, 2, 0]}},
        {"name": "bytes", "configuration": {"endian": "little"}},
    ]


def reachable(url: str) -> bool:
    """Whether the host serving ``url`` answers at all.

    A server that is down is not a failure of this parser, so the remote test
    skips rather than reporting one; anything the server does answer is a
    result and is asserted on.
    """
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
    assert index.size == 101048320
    assert index.requests <= 4
    # Anything approaching a frame (25 MB) would mean pixels were fetched.
    assert index.fetched_bytes < 1 << 20


def test_index_local_matches_remote():
    check_index(index_nd2(str(local_copy())))


def test_frame_bytes_identical_to_nd2(tmp_path):
    """A position read through Icechunk equals what ``nd2`` decodes itself.

    This is the claim the whole parser rests on: the ``transpose`` codec
    presents the file's interleaved ``(y, x, c)`` bytes as a ``(c, y, x)``
    array, with nothing rewritten.
    """
    import nd2
    from minus80.core.icechunk_io import create_repo, open_repo
    from minus80.core.icechunk_io import commit
    from virtualizarr.manifests import ManifestGroup

    path = local_copy()
    index = index_nd2(str(path))
    arrays = {f"p{p}": index.position_array(p) for p in range(index.positions)}
    arrays["stacked"] = index.stacked_array()

    repo = create_repo(tmp_path / "repo", [index.url])
    session = repo.writable_session("main")
    ManifestGroup(arrays=arrays).to_icechunk(session.store)
    commit(session, "one ND2")

    with nd2.ND2File(str(path)) as f:
        truth = f.asarray()                      # (position, c, y, x)
    root = open_repo(tmp_path / "repo")
    for p in range(index.positions):
        assert np.array_equal(root[f"p{p}"][:], truth[p])
    assert np.array_equal(root["stacked"][:], truth)


def test_stacked_array_reuses_the_same_bytes():
    index = index_nd2(str(local_copy()))
    stacked = index.stacked_array()
    assert stacked.shape == (index.positions, *index.frame_shape)
    assert stacked.metadata.chunks == (1, *index.frame_shape)
    assert stacked.metadata.dimension_names == ("position", "c", "y", "x")
    per_position = [index.position_array(p).manifest.get_entry((0, 0, 0))["offset"]
                    for p in range(index.positions)]
    assert [stacked.manifest.get_entry((p, 0, 0, 0))["offset"]
            for p in range(index.positions)] == per_position


def test_refuses_compressed():
    """A compressed ND2 has no Zarr codec, so it is refused rather than mangled."""
    attributes = SimpleNamespace(compressionType="lossless", componentCount=3,
                                 heightPx=2048, widthPx=2048, widthBytes=12288)
    source = SimpleNamespace(attributes=attributes, dtype="uint16")
    ranges = SimpleNamespace(url=SMALL, size=0, requests=0, bytes=0)
    with pytest.raises(UnsupportedND2, match="compressed"):
        _describe(source, {}, ranges)


def test_refuses_padded_rows():
    """A row wider than its pixels is not a Zarr chunk, so it is refused too."""
    attributes = SimpleNamespace(compressionType=None, componentCount=3,
                                 heightPx=2048, widthPx=2048, widthBytes=16384)
    source = SimpleNamespace(attributes=attributes, dtype="uint16")
    ranges = SimpleNamespace(url=SMALL, size=0, requests=0, bytes=0)
    with pytest.raises(UnsupportedND2, match="padded rows"):
        _describe(source, {}, ranges)


def test_refuses_a_loop_that_is_not_stage_positions():
    """A timelapse would be presented as fields of view, so it is refused."""
    attributes = SimpleNamespace(compressionType=None, componentCount=3,
                                 heightPx=2048, widthPx=2048, widthBytes=12288)
    source = SimpleNamespace(attributes=attributes, dtype="uint16",
                             experiment=[SimpleNamespace(type="XYPosLoop"),
                                         SimpleNamespace(type="TimeLoop")])
    ranges = SimpleNamespace(url=SMALL, size=0, requests=0, bytes=0)
    with pytest.raises(UnsupportedND2, match="TimeLoop"):
        _describe(source, {}, ranges)
