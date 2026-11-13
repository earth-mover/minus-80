"""A Zarr v2 store zipped with Deflate, opened in place and read through zarr."""

import zipfile

import numpy as np
import pytest
import zarr
from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype

from minus80.core.aio import run_sync
from minus80.parsers.zip.reading import read_central_directory
from minus80.parsers.zip.store import MemberStore


@pytest.fixture
def zipped(tmp_path):
    """A Zarr v2 group with one array whose last chunk is missing, zipped with Deflate."""
    root = tmp_path / "store.zarr"
    data = np.arange(6 * 7, dtype="<u2").reshape(6, 7)
    data[4:, 4:] = 0
    arr = zarr.create_array(
        str(root),
        name="a",
        shape=data.shape,
        chunks=(4, 4),
        dtype=data.dtype,
        zarr_format=2,
        fill_value=0,
        config={"write_empty_chunks": False},
        chunk_key_encoding={"name": "v2", "separator": "/"},
    )
    arr[:] = data
    arr.attrs["note"] = "kept"
    path = tmp_path / "store.zarr.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(root).as_posix())
    return path, data


def test_reads_values_through_zarr(zipped):
    path, data = zipped
    directory = read_central_directory(path.as_uri())
    store = MemberStore(path.as_uri(), directory)
    arr = zarr.open_array(store, path="a", zarr_format=2, mode="r")
    assert "a/1/1" not in directory["name"].to_pylist()
    np.testing.assert_array_equal(arr[:], data)
    np.testing.assert_array_equal(arr[1:5, 2:6], data[1:5, 2:6])
    assert arr.attrs["note"] == "kept"


def test_lists_like_a_directory(zipped):
    path, _ = zipped
    store = MemberStore(path.as_uri(), read_central_directory(path.as_uri()))

    async def listed(prefix):
        return sorted([k async for k in store.list_dir(prefix)])

    assert "a" in run_sync(listed(""))
    assert run_sync(listed("a")) == [".zarray", ".zattrs", "0", "1"]


def test_byte_ranges_apply_to_the_inflated_member(zipped):
    path, _ = zipped
    store = MemberStore(path.as_uri(), read_central_directory(path.as_uri()))
    whole = zipfile.ZipFile(path).read("a/.zarray")
    proto = default_buffer_prototype()
    for request, expected in [
        (RangeByteRequest(2, 9), whole[2:9]),
        (OffsetByteRequest(5), whole[5:]),
        (SuffixByteRequest(4), whole[-4:]),
    ]:
        assert run_sync(store.get("a/.zarray", proto, request)).to_bytes() == expected
    assert run_sync(store.get("missing", proto)) is None


def test_a_member_that_fails_its_crc_raises(zipped):
    import pyarrow as pa
    import pyarrow.compute as pc

    path, _ = zipped
    directory = read_central_directory(path.as_uri())
    wrong = pc.if_else(pc.equal(directory["name"], "a/0/0"), pa.scalar(0, pa.uint32()), directory["crc32"])
    directory = directory.set_column(directory.schema.get_field_index("crc32"), "crc32", wrong)
    arr = zarr.open_array(MemberStore(path.as_uri(), directory), path="a", zarr_format=2, mode="r")
    with pytest.raises(OSError, match="CRC"):
        arr[0, 0]


def test_metadata_names_keep_metadata_and_named_paths():
    from minus80.parsers.zip.store import metadata_names

    names = ["a/.zarray", "a/0/0", "b/zarr.json", "b/c/0/0", "t/tables/x/0", ".zgroup"]
    assert set(metadata_names(names)) == {"a/.zarray", "b/zarr.json", ".zgroup"}
    assert "t/tables/x/0" in metadata_names(names, also=["tables"])


def test_an_empty_member_reads_as_a_missing_chunk(tmp_path):
    """A zero-length chunk file reads as the fill value, as a missing chunk does."""
    root = tmp_path / "store.zarr"
    arr = zarr.create_array(
        str(root),
        name="a",
        shape=(4, 4),
        chunks=(2, 2),
        dtype="<u2",
        zarr_format=2,
        fill_value=7,
        chunk_key_encoding={"name": "v2", "separator": "/"},
    )
    arr[:] = 1
    (root / "a" / "1" / "1").write_bytes(b"")
    path = tmp_path / "store.zarr.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(root).as_posix())
    got = zarr.open_array(MemberStore(path.as_uri(), read_central_directory(path.as_uri())), path="a", mode="r")[
        :
    ]
    assert (got[2:, 2:] == 7).all() and (got[:2, :2] == 1).all()


def test_a_member_that_fails_the_check_reads_as_missing_and_is_recorded(zipped):
    path, data = zipped

    def check(name, raw):
        return "rejected for the test" if name == "a/0/0" else None

    store = MemberStore(path.as_uri(), read_central_directory(path.as_uri()), check=check)
    got = zarr.open_array(store, path="a", mode="r")[:]
    assert (got[:4, :4] == 0).all()
    np.testing.assert_array_equal(got[4:], data[4:])
    assert store.rejected == {"a/0/0": "rejected for the test"}
