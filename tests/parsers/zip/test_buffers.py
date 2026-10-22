"""Zip structures read from buffers, against archives written by ``zipfile``."""
import io
import zipfile

import numpy as np
import pytest
from obstore.store import MemoryStore

from minus80.parsers.zip.buffers import (end_of_central_directory, read_member, scan_local_headers, walk_archive,
                                 walk_local_headers)
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
def test_walk_finds_every_member_and_reads_it(compression, force_zip64):
    archive = _archive(MEMBERS, compression, force_zip64)
    found = {m.name: m for m in walk_local_headers(archive)}
    assert list(found) == ["dir/", *MEMBERS]
    for name, data in MEMBERS.items():
        assert read_member(archive, found[name]) == data
    stored = found["dir/m0.bin"].entry
    if compression == zipfile.ZIP_STORED:
        assert archive[stored.data_offset:stored.data_offset + stored.compressed_length] == MEMBERS["dir/m0.bin"]
    assert stored.uncompressed_length == len(MEMBERS["dir/m0.bin"])
    assert stored.method == compression


def test_walk_stops_where_a_member_runs_past_the_buffer():
    archive = _archive(MEMBERS)
    third = list(walk_local_headers(archive))[3]
    cut = archive[:third.entry.data_offset + 10]
    assert [m.name for m in walk_local_headers(cut)] == ["dir/", "dir/m0.bin", "dir/m1.bin"]


def test_walk_stops_at_the_central_directory():
    archive = _archive(MEMBERS)
    assert len(list(walk_local_headers(archive))) == len(MEMBERS) + 1


def test_scan_from_the_middle_of_an_archive_reports_archive_offsets():
    archive = _archive(MEMBERS)
    whole = {m.name: m for m in walk_local_headers(archive)}
    start = whole["dir/m2.bin"].entry.data_offset + 5
    window = archive[start:]
    found = {m.name: m for m in scan_local_headers(window, offset=start)}
    assert "dir/m2.bin" not in found
    assert found["dir/m3.bin"] == whole["dir/m3.bin"]
    assert read_member(window, found["dir/m4.bin"], offset=start) == MEMBERS["dir/m4.bin"]


def test_walk_refuses_a_data_descriptor():
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


def test_end_record_of_a_small_archive():
    archive = _archive(MEMBERS)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        expected = (len(zf.infolist()), zf.start_dir)
    record = end_of_central_directory(archive[-64:], len(archive))
    assert (record.entries, record.offset) == expected
    assert record.offset + record.size <= len(archive)


def test_end_record_of_a_zip64_archive_from_the_tail_alone():
    # More than 65,535 members forces the ZIP64 end record.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(70_000):
            zf.writestr(f"m{i}", b"")
    archive = buf.getvalue()
    assert b"PK\x06\x06" in archive[-200:]
    record = end_of_central_directory(archive[-1024:], len(archive))
    assert record.entries == 70_000
    assert archive[record.offset:record.offset + 4] == b"PK\x01\x02"
    assert record.offset + record.size < len(archive)


def test_end_record_missing_raises():
    with pytest.raises(zipfile.BadZipFile):
        end_of_central_directory(b"not a zip" * 20, 180)


def _stored(archive: bytes) -> tuple[MemoryStore, int]:
    store = MemoryStore()
    store.put("a.zip", archive)
    return store, end_of_central_directory(archive[-(64 << 10):], len(archive)).offset


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_walk_archive_across_pieces(compression):
    archive = _archive(MEMBERS, compression, force_zip64=True)
    store, end = _stored(archive)
    # Pieces smaller than a member, so every member is carried across a piece boundary.
    walked = list(walk_archive(store, "a.zip", end, piece=200, prefetch=3))
    assert [w.member.name for w in walked] == ["dir/", *MEMBERS]
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        assert [w.header_offset for w in walked] == [i.header_offset for i in zf.infolist()]
    for w in walked[1:]:
        assert member_bytes(bytes(w.data), w.member.entry) == MEMBERS[w.member.name]


def test_walk_archive_refuses_an_end_inside_a_member():
    store, end = _stored(_archive(MEMBERS))
    with pytest.raises(ValueError, match="short of"):
        list(walk_archive(store, "a.zip", end - 10, piece=200))


def test_walk_archive_refuses_bytes_that_are_not_a_header():
    store = MemoryStore()
    store.put("a.zip", b"\0" * 1000)
    with pytest.raises(ValueError, match="not a local header"):
        list(walk_archive(store, "a.zip", 1000, piece=200, max_carry=500))
