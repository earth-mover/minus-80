import io
import subprocess
import sys
from functools import partial
from pathlib import Path

import numpy as np
import pytest

from minus80.core import fetch
from minus80.core.fetch import iter_ranges, object_size, read_range, sparse_local
from minus80.core.progress import Progress

DATA = np.random.default_rng(0).integers(0, 256, 12_000, dtype=np.uint8).tobytes()


@pytest.fixture
def url(range_server):
    range_server.files["/d/f.bin"] = DATA
    return f"{range_server.url}/d/f.bin"


def test_adjacent_blocks_are_one_request_each(url, range_server):
    got = list(iter_ranges(url, [(0, 1000), (1000, 2000), (2000, 3000)], progress=False))
    assert [s for s, _ in got] == [0, 1000, 2000]
    assert b"".join(d for _, d in got) == DATA[:3000]
    assert sorted(range_server.gets()) == ["bytes=0-999", "bytes=1000-1999", "bytes=2000-2999"]


def test_a_range_longer_than_a_block_is_split_in_order(url, range_server):
    got = list(iter_ranges(url, [(100, 10_100)], block=4096, concurrency=2, progress=False))
    assert [s for s, _ in got] == [100, 4196, 8292]
    assert b"".join(d for _, d in got) == DATA[100:10_100]
    assert len(range_server.gets()) == 3


def test_a_short_response_raises(url, range_server):
    range_server.short = 1
    with pytest.raises(OSError, match="short range read"):
        list(iter_ranges(url, [(0, 100)], progress=False))
    with pytest.raises(OSError, match="short range read"):
        read_range(url, 0, 100)


def test_an_error_leaves_no_request_holding_the_process_open(url, range_server):
    # The GETs still in flight when the first piece fails go to a server that
    # never answers them; a patient store would wait 300 s on each, then retry.
    range_server.short = 1
    range_server.stall_from = 200
    script = (
        "from minus80.core.fetch import iter_ranges\n"
        f"pieces = iter_ranges({url!r}, [(0, 4000)], block=200, concurrency=8,\n"
        "                     progress=False, patient=True)\n"
        "try:\n"
        "    list(pieces)\n"
        "except OSError:\n"
        "    pass\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)


def test_a_hanging_piece_is_asked_for_again_while_later_pieces_are_fetched(url, range_server, capsys):
    range_server.stall_once = {0}
    got = list(iter_ranges(url, [(0, 8000)], block=1000, concurrency=2, window=8,
                           hedge_after=0.5, progress=False))
    assert b"".join(d for _, d in got) == DATA[:8000]
    gets = range_server.gets()
    # The first piece's second request comes after every later piece was asked for.
    assert gets.count("bytes=0-999") == 2
    assert gets[-1] == "bytes=0-999"
    assert len(gets) == 9
    assert "slow range [0, 1,000) of f.bin" in capsys.readouterr().out


def test_read_range_is_one_request(url, range_server):
    assert read_range(url, 10, 5000) == DATA[10:5000]
    assert range_server.gets() == ["bytes=10-4999"]


def test_object_size_is_one_head(url, range_server):
    assert object_size(url) == len(DATA)
    assert [m for m, _, _ in range_server.requests] == ["HEAD"]


def test_progress_names_the_file_then_counts(url, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(fetch, "Progress", partial(Progress, every=0, stream=out))
    list(iter_ranges(url, [(0, 4096)], block=2048))
    lines = out.getvalue().splitlines()
    assert lines[0].strip().startswith("fetching f.bin")
    assert "of" in lines[1] and "%" in lines[1]


def test_sparse_local_writes_only_the_regions(url, tmp_path, range_server):
    regions = [(6000, 7000), (100, 200)]
    path = sparse_local(url, regions, str(tmp_path))
    local = Path(path).read_bytes()
    assert len(local) == len(DATA)
    expected = bytearray(len(DATA))
    for a, b in regions:
        expected[a:b] = DATA[a:b]
    assert local == bytes(expected)
    assert sorted(range_server.gets()) == ["bytes=100-199", "bytes=6000-6999"]


def test_sparse_local_keys_its_cache_on_the_regions(url, tmp_path):
    first = sparse_local(url, [(0, 10)], str(tmp_path))
    assert sparse_local(url, [(0, 10)], str(tmp_path)) == first
    assert sparse_local(url, [(10, 20)], str(tmp_path)) != first


def test_cache_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MINUS80_CACHE_DIR", str(tmp_path / "m80"))
    assert fetch.cache_dir() == str(tmp_path / "m80")
    monkeypatch.delenv("MINUS80_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert fetch.cache_dir() == str(tmp_path / "xdg" / "minus80")


def test_download_sends_one_request_per_block(url, tmp_path, range_server):
    path = fetch.download_then_index(url, str(tmp_path), block=4096, concurrency=2)
    assert Path(path).read_bytes() == DATA
    assert sorted(range_server.gets()) == ["bytes=0-4095", "bytes=4096-8191", "bytes=8192-11999"]
