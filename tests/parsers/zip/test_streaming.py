"""Members cut out of consecutive ranges of a zip, on zips written with and without data descriptors.

The cutter is tested on ranges sliced from the file's bytes; streaming, through a ``file://`` URL.
"""

import io
import zipfile

import numpy as np
import pytest

from minus80.parsers.zip.directory import inflate_member, parse_central_directory
from minus80.parsers.zip.reading import stream_members
from minus80.parsers.zip.streaming import MemberCutter, member_regions, spans_in_file_order


class _Unseekable(io.RawIOBase):
    """A write-only stream: zipfile then writes every member with a data descriptor."""

    def __init__(self, f):
        self._f = f

    def writable(self):
        return True

    def write(self, b):
        return self._f.write(b)


def _members():
    rng = np.random.default_rng(1)
    out = {"a/.zarray": b'{"zarr_format": 2}'}
    out |= {f"a/{i}": rng.integers(0, 50, 3000 + 97 * i, dtype=np.uint16).tobytes() for i in range(9)}
    out["a/empty"] = b""
    out["b/" + "x" * 300] = b"a member whose long name pushes its data past a small range" * 3
    return out


def _zip(path, members, descriptors=True):
    with open(path, "wb") as f:
        with zipfile.ZipFile(_Unseekable(f) if descriptors else f, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members.items():
                with zf.open(name, "w") as w:
                    w.write(data)
    data = path.read_bytes()
    return data, parse_central_directory(data, len(data))


def _cut(data, rows, piece):
    cutter = MemberCutter(rows)
    regions = member_regions(rows)
    got = []
    for start, end in regions:
        for offset in range(start, end, piece):
            got += cutter.feed(offset, data[offset:min(offset + piece, end)])
    cutter.finish()
    return got


@pytest.mark.parametrize("descriptors", [True, False])
@pytest.mark.parametrize("piece", [1, 7, 1000, 1 << 30])
def test_every_member_comes_out_once_in_file_order(tmp_path, descriptors, piece):
    """Ranges as small as one byte cut members across range ends, and the cutter still returns each once."""
    members = _members()
    data, directory = _zip(tmp_path / "a.zip", members, descriptors)
    rows = spans_in_file_order(directory, members)
    got = _cut(data, rows, piece)
    assert [m.name for m in got] == list(members)
    for member in got:
        assert inflate_member(member.stored, member.entry) == members[member.name]


def test_a_subset_skips_the_other_members(tmp_path):
    members = _members()
    data, directory = _zip(tmp_path / "a.zip", members)
    wanted = ["a/1", "a/2", "a/6", "a/empty"]
    rows = spans_in_file_order(directory, wanted)
    # a/1 and a/2 are neighbors, so they share one region; a/6 and a/empty do not.
    assert member_regions(rows) == [
        (rows[0]["start"], rows[1]["end"]),
        (rows[2]["start"], rows[2]["end"]),
        (rows[3]["start"], rows[3]["end"]),
    ]
    got = _cut(data, rows, 500)
    assert [m.name for m in got] == wanted


def test_ranges_out_of_order_or_missing_raise(tmp_path):
    members = _members()
    data, directory = _zip(tmp_path / "a.zip", members)
    rows = spans_in_file_order(directory, members)
    cutter = MemberCutter(rows)
    cutter.feed(0, data[:100])
    with pytest.raises(ValueError, match="starts at byte"):
        cutter.feed(200, data[200:300])
    short = MemberCutter(rows)
    short.feed(0, data[: rows[3]["end"]])
    with pytest.raises(ValueError, match="never arrived"):
        short.finish()


def test_stream_members_reads_the_file_in_ranges(tmp_path):
    members = _members()
    path = tmp_path / "a.zip"
    _, directory = _zip(path, members)
    rows = spans_in_file_order(directory, members)
    got = list(stream_members(path.as_uri(), rows, range_bytes=4096, concurrency=3, progress=False))
    assert {m.name: inflate_member(m.stored, m.entry) for m in got} == members
