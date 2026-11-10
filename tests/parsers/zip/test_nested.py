"""Zips inside zips, opened as seekable files without extracting the inner one."""

import zipfile

import pytest

from minus80.core.remote import reader
from minus80.parsers.zip.nested import member_file, member_zip

INNER_BIN = bytes(i % 256 for i in range(100_000))


@pytest.fixture
def outer(tmp_path):
    inner = tmp_path / "inner.zarr.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("a/.zarray", b"{}")
        zf.writestr("a/0.0", b"chunk")
    path = tmp_path / "outer.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("inner.bin", INNER_BIN)
        zf.write(inner, "inner.zarr.zip")
        zf.writestr("deflated.bin", INNER_BIN, compress_type=zipfile.ZIP_DEFLATED)
    return path


def test_member_file_reads_a_stored_member_by_range(outer):
    with open(outer, "rb") as archive, member_file(archive, "inner.bin") as f:
        f.seek(65_536)
        assert f.read(4) == bytes([0, 1, 2, 3])
        f.seek(-3, 2)
        assert f.read() == bytes([157, 158, 159])
        f.seek(0)
        assert f.read() == INNER_BIN


def test_member_zip_opens_a_zip_inside_a_zip(outer):
    with open(outer, "rb") as archive, member_zip(archive, "inner.zarr.zip") as zf:
        assert sorted(zf.namelist()) == ["a/.zarray", "a/0.0"]
        assert zf.read("a/0.0") == b"chunk"


def test_a_deflated_member_is_refused(outer):
    with open(outer, "rb") as archive, pytest.raises(NotImplementedError, match="deflate"):
        member_file(archive, "deflated.bin")


def test_member_zip_over_a_remote_outer_file(range_server, outer):
    range_server.files["/outer.zip"] = outer.read_bytes()
    with member_zip(reader(f"{range_server.url}/outer.zip"), "inner.zarr.zip") as zf:
        assert zf.read("a/0.0") == b"chunk"
    assert all(r is not None for r in range_server.gets())
