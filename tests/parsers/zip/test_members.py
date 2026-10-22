"""Zips written by zipfile, referenced member by member, read back through Icechunk."""

import io
import zipfile

import numpy as np
import pytest
import tifffile
import zarr
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


def _roundtrip(tmp_path, path, arrays):
    repo_path = tmp_path / "repo"
    repo = create_repo(repo_path, [path.as_uri()])
    session = repo.writable_session("main")
    ManifestGroup(arrays=arrays).to_icechunk(session.store)
    commit(session, "test")
    return open_repo(repo_path)


@pytest.mark.parametrize("force_zip64", [False, True])
def test_frames_read_back(tmp_path, force_zip64):
    frames = _frames(4, (3, 37, 29))
    path = tmp_path / "data.zip"
    _write_zip(path, {f"seq/t{t:03d}.tif": _tiff(f) for t, f in enumerate(frames)}, force_zip64=force_zip64)
    index = zip_index(path.as_uri())
    assert set(index) == {f"seq/t{t:03d}.tif" for t in range(4)}
    assert read_member(path.as_uri(), index["seq/t002.tif"]) == zipfile.ZipFile(path).read("seq/t002.tif")
    arr = member_array(path.as_uri(), index, np.array([f"seq/t{t:03d}.tif" for t in range(4)], dtype=object).reshape(4, 1, 1, 1),
                       shape=(4, 3, 37, 29), chunk_shape=(1, 3, 37, 29), dtype="uint16", serializer=TIFF,
                       dimension_names=("t", "z", "y", "x"))
    assert [c["name"] for c in arr.metadata.to_dict()["codecs"]] == ["imagecodecs_tiff"]
    got = _roundtrip(tmp_path, path, {"image": arr})["image"][:]
    np.testing.assert_array_equal(got, np.stack(frames))


def test_missing_members_read_as_fill(tmp_path):
    frames = _frames(2, (16, 16))
    path = tmp_path / "masks.zip"
    _write_zip(path, {"seq/m000.tif": _tiff(frames[0]), "seq/m002.tif": _tiff(frames[1])})
    index = zip_index(path.as_uri())
    arr = member_array(path.as_uri(), index, np.array(["seq/m000.tif", None, "seq/m002.tif"], dtype=object).reshape(3, 1, 1),
                       shape=(3, 16, 16), chunk_shape=(1, 16, 16), dtype="uint16", serializer=TIFF)
    got = _roundtrip(tmp_path, path, {"mask": arr})["mask"][:]
    np.testing.assert_array_equal(got[0], frames[0])
    assert not got[1].any()
    np.testing.assert_array_equal(got[2], frames[1])


def test_deflate_members_raise(tmp_path):
    path = tmp_path / "deflated.zip"
    _write_zip(path, {"seq/t0.tif": _tiff(_frames(1, (8, 8))[0])}, compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(NotImplementedError, match="only STORED"):
        member_array(path.as_uri(), zip_index(path.as_uri()), np.array(["seq/t0.tif"], dtype=object).reshape(1, 1, 1),
                     shape=(1, 8, 8), chunk_shape=(1, 8, 8), dtype="uint16", serializer=TIFF)


def test_unknown_member_raises(tmp_path):
    path = tmp_path / "one.zip"
    _write_zip(path, {"seq/a.tif": _tiff(_frames(1, (4, 4))[0])})
    with pytest.raises(KeyError):
        member_array(path.as_uri(), zip_index(path.as_uri()), np.array(["seq/b.tif"], dtype=object).reshape(1, 1, 1),
                     shape=(1, 4, 4), chunk_shape=(1, 4, 4), dtype="uint16", serializer=TIFF)


def test_inflate_prefix_stops_early(tmp_path):
    data = np.random.default_rng(1).integers(0, 255, 2_000_000, dtype=np.uint8).tobytes()
    path = tmp_path / "big.zip"
    _write_zip(path, {"seq/big.bin": data}, compression=zipfile.ZIP_DEFLATED)
    entry = zip_index(path.as_uri())["seq/big.bin"]
    prefix, pulled = inflate_prefix(path.as_uri(), entry, 100_000, block=65536)
    assert prefix[:100_000] == data[:100_000]
    assert pulled < entry.compressed_length / 4
