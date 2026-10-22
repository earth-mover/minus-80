"""Zips written by zipfile, referenced member by member, read back through Icechunk."""

import asyncio
import io
import zipfile

import numpy as np
import pytest
import tifffile
from virtualizarr.manifests import ManifestGroup

from minus80.core.icechunk_io import commit, create_repo, open_repo
from minus80.parsers.zip.members import inflate_prefix, member_array, read_member, zip_index

TIFF = {"name": "imagecodecs_tiff"}


def _tiff(data: np.ndarray) -> bytes:
    buf = io.BytesIO()
    tifffile.imwrite(buf, data, compression="lzw", rowsperstrip=4, photometric="minisblack")
    return buf.getvalue()


def _frames(n, shape, seed=0):
    rng = np.random.default_rng(seed)
    return [rng.integers(0, 4000, shape, dtype=np.uint16) for _ in range(n)]


def _write_zip(path, members: dict[str, bytes], *, compression=zipfile.ZIP_STORED, force_zip64=False):
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        zf.mkdir("seq")
        for name, data in members.items():
            with zf.open(name, "w", force_zip64=force_zip64) as f:
                f.write(data)


def _grid(*names):
    return np.array(names, dtype=object).reshape(len(names), 1, 1)


def _roundtrip(tmp_path, path, arrays):
    repo_path = tmp_path / "repo"
    repo = create_repo(repo_path, [path.as_uri()])
    session = repo.writable_session("main")
    ManifestGroup(arrays=arrays).to_icechunk(session.store)
    commit(session, "test")
    return open_repo(repo_path)


@pytest.mark.parametrize("force_zip64", [False, True])
def test_frames_read_back_as_tifffile_decodes_them(tmp_path, force_zip64):
    frames = _frames(4, (3, 37, 29))
    path = tmp_path / "data.zip"
    _write_zip(path, {f"seq/t{t:03d}.tif": _tiff(f) for t, f in enumerate(frames)}, force_zip64=force_zip64)
    names = [f"seq/t{t:03d}.tif" for t in range(4)]
    arr = member_array(path, np.array(names, dtype=object).reshape(4, 1, 1, 1),
                       shape=(4, 3, 37, 29), chunk_shape=(1, 3, 37, 29), dtype="uint16", serializer=TIFF,
                       dimension_names=("t", "z", "y", "x"))
    assert arr.metadata.attributes["source_url"] == path.as_uri()
    got = _roundtrip(tmp_path, path, {"image": arr})["image"][:]
    with zipfile.ZipFile(path) as zf:
        expected = np.stack([tifffile.imread(io.BytesIO(zf.read(n))) for n in names])
    np.testing.assert_array_equal(got, expected)


def test_absent_and_zero_length_members_read_as_fill(tmp_path):
    frames = _frames(2, (16, 16))
    path = tmp_path / "masks.zip"
    _write_zip(path, {"seq/m0.tif": _tiff(frames[0]), "seq/m2.tif": _tiff(frames[1]), "seq/empty.tif": b""})
    arr = member_array(path, _grid("seq/m0.tif", None, "seq/m2.tif", "seq/empty.tif"),
                       shape=(4, 16, 16), chunk_shape=(1, 16, 16), dtype="uint16", serializer=TIFF)
    got = _roundtrip(tmp_path, path, {"mask": arr})["mask"][:]
    np.testing.assert_array_equal(got[[0, 2]], np.stack(frames))
    assert not got[[1, 3]].any()


def test_an_open_file_indexes_like_its_path_and_needs_url(tmp_path):
    path = tmp_path / "one.zip"
    _write_zip(path, {"seq/a.tif": _tiff(_frames(1, (4, 4))[0])})
    kw = dict(shape=(1, 4, 4), chunk_shape=(1, 4, 4), dtype="uint16", serializer=TIFF)
    with open(path, "rb") as f:
        assert zip_index(f) == zip_index(path)
        with pytest.raises(TypeError, match="url"):
            member_array(f, _grid("seq/a.tif"), **kw)
        from_file = member_array(f, _grid("seq/a.tif"), url="s3://bucket/one.zip", **kw)
    from_path = member_array(path, _grid("seq/a.tif"), **kw).manifest.dict()
    assert from_file.manifest.dict() == {k: {**v, "path": "s3://bucket/one.zip"} for k, v in from_path.items()}


def test_an_http_source_indexes_like_the_local_file(tmp_path, range_server):
    path = tmp_path / "one.zip"
    _write_zip(path, {"seq/a.tif": _tiff(_frames(1, (4, 4))[0])})
    range_server.files["/one.zip"] = path.read_bytes()
    url = f"{range_server.url}/one.zip"
    index = zip_index(url)
    assert index == zip_index(path)
    assert read_member(url, index["seq/a.tif"]) == zipfile.ZipFile(path).read("seq/a.tif")


def test_deflate_members_raise(tmp_path):
    path = tmp_path / "deflated.zip"
    _write_zip(path, {"seq/t0.tif": _tiff(_frames(1, (8, 8))[0])}, compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(NotImplementedError, match="only STORED"):
        member_array(path, _grid("seq/t0.tif"), shape=(1, 8, 8), chunk_shape=(1, 8, 8), dtype="uint16", serializer=TIFF)


def test_unknown_member_raises(tmp_path):
    path = tmp_path / "one.zip"
    _write_zip(path, {"seq/a.tif": _tiff(_frames(1, (4, 4))[0])})
    with pytest.raises(KeyError):
        member_array(path, _grid("seq/b.tif"), shape=(1, 4, 4), chunk_shape=(1, 4, 4), dtype="uint16", serializer=TIFF)


def test_inflate_prefix_stops_early(tmp_path):
    data = np.random.default_rng(1).integers(0, 255, 2_000_000, dtype=np.uint8).tobytes()
    path = tmp_path / "big.zip"
    _write_zip(path, {"seq/big.bin": data}, compression=zipfile.ZIP_DEFLATED)
    entry = zip_index(path)["seq/big.bin"]
    prefix, pulled = inflate_prefix(path, entry, 100_000, block=65536)
    assert prefix[:100_000] == data[:100_000]
    assert pulled < entry.compressed_length / 4


def test_zip_index_inside_a_running_loop(tmp_path):
    path = tmp_path / "one.zip"
    _write_zip(path, {"seq/a.tif": _tiff(_frames(1, (4, 4))[0])})

    async def in_a_notebook():
        return zip_index(path)

    assert list(asyncio.run(in_a_notebook())) == ["seq/a.tif"]
