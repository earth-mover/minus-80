"""Zip structures read from buffers, against archives written by ``zipfile``."""
import io
import zipfile

import numpy as np
import pytest

from minus80.parsers.zip.buffers import (end_of_central_directory, member_from_buffer, scan_local_headers,
                                         walk_archive, walk_local_headers)
from minus80.parsers.zip.members import member_bytes


def _archive(members: dict[str, bytes], compression=zipfile.ZIP_STORED, force_zip64=False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        zf.mkdir("dir")
        for name, data in members.items():
            with zf.open(name, "w", force_zip64=force_zip64) as f:
                f.write(data)
    return buf.getvalue()


MEMBERS = {f"dir/m{i}.bin": np.random.default_rng(i).bytes(500 + 37 * i) for i in range(6)}


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
@pytest.mark.parametrize("force_zip64", [False, True])
def test_walk_finds_every_member_up_to_the_central_directory(compression, force_zip64):
    archive = _archive(MEMBERS, compression, force_zip64)
    found = {m.name: m for m in walk_local_headers(archive)}
    assert list(found) == ["dir/", *MEMBERS]
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for name in MEMBERS:
            assert member_from_buffer(archive, found[name]) == zf.read(name)


def test_walk_stops_where_a_member_runs_past_the_buffer():
    archive = _archive(MEMBERS)
    third = list(walk_local_headers(archive))[3]
    cut = archive[:third.entry.data_offset + 10]
    assert [m.name for m in walk_local_headers(cut)] == ["dir/", "dir/m0.bin", "dir/m1.bin"]


def test_scan_from_the_middle_of_an_archive_reports_archive_offsets():
    archive = _archive(MEMBERS)
    whole = {m.name: m for m in walk_local_headers(archive)}
    start = whole["dir/m2.bin"].entry.data_offset + 5
    window = archive[start:]
    found = {m.name: m for m in scan_local_headers(window, offset=start)}
    assert "dir/m2.bin" not in found
    assert found["dir/m3.bin"] == whole["dir/m3.bin"]
    assert member_from_buffer(window, found["dir/m4.bin"], offset=start) == MEMBERS["dir/m4.bin"]


def test_walk_refuses_a_data_descriptor():
    # zipfile puts the sizes in a data descriptor when it cannot seek back.
    class Unseekable(io.RawIOBase):
        def __init__(self):
            self.buf = io.BytesIO()
        def writable(self): return True
        def write(self, b): return self.buf.write(b)

    sink = Unseekable()
    with zipfile.ZipFile(sink, "w") as zf:
        zf.writestr("a.bin", b"abc" * 100)
    with pytest.raises(ValueError, match="data descriptor"):
        list(walk_local_headers(sink.buf.getvalue()))


def test_end_record_matches_zipfile():
    archive = _archive(MEMBERS)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        expected = (len(zf.infolist()), zf.start_dir)
    record = end_of_central_directory(archive[-64:], len(archive))
    assert (record.entries, record.offset) == expected


def test_end_record_of_a_zip64_archive_from_the_tail_alone():
    # More than 65,535 members forces the ZIP64 end record, whose locator
    # zipfile checks against the absolute position it was read from.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(70_000):
            zf.writestr(f"m{i}", b"")
    archive = buf.getvalue()
    assert b"PK\x06\x06" in archive[-200:]
    record = end_of_central_directory(archive[-1024:], len(archive))
    assert record.entries == 70_000
    assert archive[record.offset:record.offset + 4] == b"PK\x01\x02"


def test_end_record_missing_raises():
    with pytest.raises(zipfile.BadZipFile):
        end_of_central_directory(b"not a zip" * 20, 180)


@pytest.fixture(params=["path", "file", "http"])
def source_of(request, tmp_path, range_server):
    """Turns archive bytes into each kind of source ``walk_archive`` accepts."""
    def make(archive: bytes):
        path = tmp_path / "a.zip"
        path.write_bytes(archive)
        if request.param == "file":
            return open(path, "rb")
        if request.param == "http":
            range_server.files["/a.zip"] = archive
            return f"{range_server.url}/a.zip"
        return path
    return make


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_walk_archive_matches_zipfile_across_pieces(source_of, compression):
    archive = _archive(MEMBERS, compression, force_zip64=True)
    # Pieces smaller than a member, so every member is carried across a piece boundary.
    walked = list(walk_archive(source_of(archive), piece=200, concurrency=3))
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        expected = [(i.filename, i.header_offset) for i in zf.infolist()]
        assert [(w.member.name, w.header_offset) for w in walked] == expected
        for w in walked[1:]:
            assert member_bytes(bytes(w.data), w.member.entry) == zf.read(w.member.name)


def test_walk_archive_refuses_an_end_inside_a_member(tmp_path):
    archive = _archive(MEMBERS)
    (tmp_path / "a.zip").write_bytes(archive)
    end = end_of_central_directory(archive, len(archive)).offset
    with pytest.raises(ValueError, match="short of"):
        list(walk_archive(tmp_path / "a.zip", end - 10, piece=200))


def test_walk_archive_refuses_bytes_that_are_not_a_header(tmp_path):
    (tmp_path / "a.zip").write_bytes(b"\0" * 1000)
    with pytest.raises(ValueError, match="not a local header"):
        list(walk_archive(tmp_path / "a.zip", 1000, piece=200, max_carry=500))


def test_walk_archive_raises_on_a_short_read(range_server):
    archive = _archive(MEMBERS)
    range_server.files["/a.zip"] = archive
    end = end_of_central_directory(archive, len(archive)).offset
    range_server.short = 1
    with pytest.raises(OSError, match="short"):
        list(walk_archive(f"{range_server.url}/a.zip", end, piece=200))
