"""The central directory as a table, and members parsed from fetched bytes, on zips written by zipfile.

Parsing is tested on bytes read from the file; reading, through a ``file://`` URL.
"""

import io
import zipfile

import numpy as np
import pytest

from minus80.parsers.zip.directory import member_spans, parse_central_directory, parse_member
from minus80.parsers.zip.reading import cached_central_directory, read_central_directory, read_members


class _Unseekable(io.RawIOBase):
    """A write-only stream: zipfile then writes every member with a data descriptor."""

    def __init__(self, f):
        self._f = f

    def writable(self):
        return True

    def write(self, b):
        return self._f.write(b)


def _members(n=12, seed=0):
    rng = np.random.default_rng(seed)
    out = {f"a/{i}/.zarray": b'{"zarr_format": 2}' for i in range(2)}
    out |= {f"a/0/{i}.0": rng.integers(0, 50, 4000, dtype=np.uint16).tobytes() for i in range(n)}
    out["a/0/empty"] = b""
    return out


def _write(path, members, *, descriptors=True, force_zip64=False):
    with open(path, "wb") as f:
        target = _Unseekable(f) if descriptors else f
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members.items():
                with zf.open(name, "w", force_zip64=force_zip64) as w:
                    w.write(data)
    return path


@pytest.mark.parametrize("descriptors", [True, False])
@pytest.mark.parametrize("force_zip64", [False, True])
def test_directory_matches_zipfile(tmp_path, descriptors, force_zip64):
    path = _write(tmp_path / "a.zip", _members(), descriptors=descriptors, force_zip64=force_zip64)
    data = path.read_bytes()
    table = parse_central_directory(data, len(data))
    infos = zipfile.ZipFile(path).infolist()
    assert table["name"].to_pylist() == [i.filename for i in infos]
    assert table["local_header_offset"].to_pylist() == [i.header_offset for i in infos]
    assert table["compressed"].to_pylist() == [i.compress_size for i in infos]
    assert table["crc32"].to_pylist() == [i.CRC for i in infos]
    assert all(bool(f & 0x08) == descriptors for f in table["flags"].to_pylist())
    meta = table.schema.metadata
    assert int(meta[b"directory_size"]) == sum(table["entry_bytes"].to_pylist())
    assert table["entry_offset"][0].as_py() == int(meta[b"directory_offset"])


def test_parse_needs_only_the_archive_end(tmp_path):
    path = _write(tmp_path / "a.zip", _members())
    data = path.read_bytes()
    whole = parse_central_directory(data, len(data))
    start = int(whole.schema.metadata[b"directory_offset"])
    assert parse_central_directory(data[start:], len(data)).equals(whole)


def test_each_member_from_its_span(tmp_path):
    members = _members()
    path = _write(tmp_path / "a.zip", members)
    data = path.read_bytes()
    table = parse_central_directory(data, len(data))
    spans = member_spans(table)
    rows = {r["name"]: r for r in table.to_pylist()}
    for name, expected in members.items():
        start, end = spans[name]
        assert parse_member(data[start:end], start, rows[name]) == expected


def test_parse_member_checks_header_and_crc(tmp_path):
    path = _write(tmp_path / "a.zip", _members())
    data = path.read_bytes()
    table = parse_central_directory(data, len(data))
    rows = {r["name"]: r for r in table.to_pylist()}
    start, end = member_spans(table)["a/0/3.0"]
    with pytest.raises(ValueError, match="no local header"):
        parse_member(data[start:end], start, rows["a/0/4.0"])
    with pytest.raises(ValueError, match="CRC"):
        parse_member(data[start:end], start, {**rows["a/0/3.0"], "crc32": 0})


def test_read_members_over_a_url(tmp_path):
    members = _members()
    path = _write(tmp_path / "a.zip", members)
    url = path.as_uri()
    table = read_central_directory(url)
    wanted = ["a/1/.zarray", "a/0/7.0", "a/0/empty"]
    assert read_members(url, wanted, table) == {n: members[n] for n in wanted}
    with pytest.raises(KeyError):
        read_members(url, ["nope"], table)


def test_cache_rejects_another_archive(tmp_path):
    path = _write(tmp_path / "a.zip", _members())
    cache = tmp_path / "cache" / "dir.parquet"
    first = cached_central_directory(path.as_uri(), cache)
    assert cached_central_directory(path.as_uri(), cache).equals(first)
    other = _write(tmp_path / "b.zip", _members(seed=1))
    with pytest.raises(ValueError, match="not"):
        cached_central_directory(other.as_uri(), cache)
    _write(path, _members(n=13))
    with pytest.raises(ValueError, match="now"):
        cached_central_directory(path.as_uri(), cache)


def test_cache_without_size_check_reads_the_file_alone(tmp_path):
    path = _write(tmp_path / "a.zip", _members())
    cache = tmp_path / "cache" / "dir.parquet"
    first = cached_central_directory(path.as_uri(), cache)
    path.unlink()
    assert cached_central_directory(path.as_uri(), cache, check_size=False).equals(first)
