"""Zarr v2 arrays copied into a v3 group, chunk bytes unchanged."""

import json
import zipfile

import numcodecs
import numpy as np
import pytest
import zarr
from zarr.dtype import VariableLengthUTF8
from zarr.storage import MemoryStore
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from minus80.core.icechunk_io import create_repo
from minus80.core.zarr_copy import copy_chunks, copy_group, create_copy, v2_spec


def _v2_zip(tmp_path, groups=None, **arrays):
    """Write each ``name -> dict(data=..., **create_array kwargs)`` as a v2 store, zipped STORED."""
    source = tmp_path / "source.zarr"
    src = zarr.open_group(source, mode="w", zarr_format=2)
    for name, attrs in (groups or {}).items():
        src.create_group(name).attrs.update(attrs)
    for name, kwargs in arrays.items():
        kwargs = dict(kwargs)
        data = kwargs.pop("data")
        attrs = kwargs.pop("attrs", None)
        if "dtype" in kwargs:
            arr = src.create_array(name, shape=data.shape, **kwargs)
            arr[:] = data
        else:
            arr = src.create_array(name, data=data, **kwargs)
        if attrs:
            arr.attrs.update(attrs)
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(source).as_posix())
    return zipfile.ZipFile(path)


def _root(tmp_path):
    session = create_repo(tmp_path / "repo", []).writable_session("main")
    return zarr.group(session.store)


def test_blosc_chunks_copy_byte_for_byte(tmp_path):
    data = np.arange(600, dtype="<i4").reshape(20, 30)
    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=data,
            chunks=(7, 11),
            compressors=numcodecs.Blosc(cname="zstd", clevel=1, shuffle=1),
        ),
    )
    root = _root(tmp_path)
    copied = copy_group(zf, "", root)
    assert copied == {"x": 9}
    np.testing.assert_array_equal(root["x"][:], data)
    assert root["x"].metadata.codecs[-1].to_dict()["configuration"]["cname"] == "zstd"
    stored = sync(root.store.get("x/c/0/0", default_buffer_prototype()))
    assert stored.to_bytes() == zf.read("x/0.0")


@pytest.mark.parametrize(
    "shuffle,name", [(0, "noshuffle"), (1, "shuffle"), (2, "bitshuffle")]
)
def test_blosc_shuffle_is_translated(tmp_path, shuffle, name):
    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=np.arange(8, dtype="<i2"),
            chunks=(8,),
            compressors=numcodecs.Blosc(cname="lz4", clevel=5, shuffle=shuffle),
        ),
    )
    root = _root(tmp_path)
    copy_group(zf, "", root)
    config = root["x"].metadata.codecs[-1].to_dict()["configuration"]
    assert config["shuffle"] == name
    assert config["clevel"] == 5
    np.testing.assert_array_equal(root["x"][:], np.arange(8, dtype="<i2"))


def test_fortran_order_reads_back_unchanged(tmp_path):
    data = np.arange(12, dtype="<f4").reshape(4, 3)
    zf = _v2_zip(
        tmp_path,
        loc=dict(
            data=data,
            chunks=(4, 3),
            order="F",
            compressors=numcodecs.Blosc(cname="lz4", shuffle=1),
        ),
    )
    root = _root(tmp_path)
    copy_group(zf, "", root)
    np.testing.assert_array_equal(root["loc"][:], data)


def test_vlen_utf8_strings_copy(tmp_path):
    names = np.array(["a", "bc", "def"], dtype=object)
    zf = _v2_zip(
        tmp_path,
        names=dict(
            data=names,
            chunks=(3,),
            dtype=VariableLengthUTF8(),
            compressors=numcodecs.Blosc(cname="lz4", shuffle=1),
        ),
    )
    root = _root(tmp_path)
    copy_group(zf, "", root)
    assert zf.read("names/.zarray").count(b"vlen-utf8") == 1
    assert list(root["names"][:]) == ["a", "bc", "def"]


def test_booleans_copy(tmp_path):
    data = np.array([True, False, True, True, False])
    zf = _v2_zip(
        tmp_path,
        flag=dict(data=data, chunks=(2,), compressors=numcodecs.Blosc(cname="zstd")),
    )
    root = _root(tmp_path)
    copy_group(zf, "", root)
    np.testing.assert_array_equal(root["flag"][:], data)


def test_a_missing_chunk_reads_as_fill(tmp_path):
    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=np.arange(1, 11, dtype="<i4"),
            chunks=(5,),
            fill_value=0,
            compressors=numcodecs.Blosc(cname="lz4"),
        ),
    )
    kept = [n for n in zf.namelist() if n != "x/1"]
    path = tmp_path / "gap.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as out:
        for n in kept:
            out.writestr(n, zf.read(n))
    root = _root(tmp_path)
    copied = copy_group(zipfile.ZipFile(path), "", root)
    assert copied == {"x": 1}
    assert list(root["x"][5:]) == [0] * 5
    assert list(root["x"][:5]) == [1, 2, 3, 4, 5]


def test_an_unknown_filter_is_refused(tmp_path):
    root = _root(tmp_path)
    zarray = {
        "shape": [4],
        "chunks": [4],
        "dtype": "<i4",
        "fill_value": 0,
        "order": "C",
        "compressor": None,
        "filters": [{"id": "delta", "dtype": "<i4"}],
        "zarr_format": 2,
    }
    with pytest.raises(NotImplementedError, match="delta"):
        create_copy(root, "x", zarray)


def test_an_unknown_compressor_is_refused(tmp_path):
    root = _root(tmp_path)
    zarray = {
        "shape": [4],
        "chunks": [4],
        "dtype": "<i4",
        "fill_value": 0,
        "order": "C",
        "compressor": {"id": "zlib", "level": 1},
        "filters": None,
        "zarr_format": 2,
    }
    with pytest.raises(NotImplementedError, match="zlib"):
        create_copy(root, "x", zarray)


def test_v2_spec_marks_vlen_utf8_as_str():
    spec = v2_spec(
        {
            "shape": [3],
            "chunks": [3],
            "dtype": "|O",
            "fill_value": None,
            "order": "C",
            "compressor": None,
            "filters": [{"id": "vlen-utf8"}],
        }
    )
    assert spec.dtype == "str"
    assert spec.shape == (3,)


def test_zattrs_become_attributes(tmp_path):
    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=np.arange(4, dtype="<i4"),
            chunks=(4,),
            compressors=numcodecs.Blosc(cname="lz4"),
            attrs={"encoding-type": "array", "n": 3},
        ),
    )
    root = _root(tmp_path)
    copy_group(zf, "", root)
    assert root["x"].attrs["encoding-type"] == "array"
    assert root["x"].attrs["n"] == 3


def test_attributes_can_be_skipped(tmp_path):
    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=np.arange(4, dtype="<i4"),
            chunks=(4,),
            compressors=numcodecs.Blosc(cname="lz4"),
            attrs={"a": 1},
        ),
    )
    root = _root(tmp_path)
    copy_group(zf, "", root, attributes=False)
    assert dict(root["x"].attrs) == {}


def test_nested_arrays_prefix_and_dimension_names(tmp_path):
    zf = _v2_zip(
        tmp_path,
        **{
            "obs/y": dict(
                data=np.arange(6, dtype="<i4").reshape(2, 3),
                chunks=(2, 3),
                compressors=numcodecs.Blosc(cname="lz4"),
            )
        },
    )
    root = _root(tmp_path)
    copied = copy_group(
        zf, "obs", root.require_group("tbl"), dimension_names={"y": ["r", "c"]}
    )
    assert copied == {"y": 1}
    assert root["tbl/y"].metadata.dimension_names == ("r", "c")
    np.testing.assert_array_equal(root["tbl/y"][:], np.arange(6).reshape(2, 3))


def test_copy_chunks_counts_members(tmp_path):
    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=np.arange(10, dtype="<i4"),
            chunks=(5,),
            compressors=numcodecs.Blosc(cname="lz4"),
        ),
    )
    root = _root(tmp_path)
    array = create_copy(root, "x", json.loads(zf.read("x/.zarray")))
    assert copy_chunks(zf, "x", array) == 2


def test_group_attributes_are_copied(tmp_path):
    lz4 = numcodecs.Blosc(cname="lz4")
    zf = _v2_zip(
        tmp_path,
        groups={
            "X": {"encoding-type": "csr_matrix", "shape": [4, 3]},
            "X/inner": {"k": "v"},
        },
        **{
            "X/data": dict(
                data=np.array([1.0, 2.0, 3.0], dtype="<f4"),
                chunks=(3,),
                compressors=lz4,
            ),
            "X/indices": dict(
                data=np.array([0, 2, 1], dtype="<i4"), chunks=(3,), compressors=lz4
            ),
            "X/indptr": dict(
                data=np.array([0, 1, 2, 3, 3], dtype="<i4"),
                chunks=(5,),
                compressors=lz4,
            ),
        },
    )
    root = _root(tmp_path)
    copied = copy_group(zf, "X", root.require_group("X"))
    assert copied == {"data": 1, "indices": 1, "indptr": 1}
    assert root["X"].attrs["encoding-type"] == "csr_matrix"
    assert root["X"].attrs["shape"] == [4, 3]
    assert root["X/inner"].attrs["k"] == "v"
    np.testing.assert_array_equal(root["X/indptr"][:], [0, 1, 2, 3, 3])
    np.testing.assert_array_equal(root["X/data"][:], [1.0, 2.0, 3.0])
    np.testing.assert_array_equal(root["X/indices"][:], [0, 2, 1])


def test_concurrent_copy_chunks_copies_every_member(tmp_path):
    data = np.arange(600, dtype="<i4").reshape(20, 30)
    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=data,
            chunks=(7, 11),
            compressors=numcodecs.Blosc(cname="zstd", clevel=1, shuffle=1),
        ),
    )
    root = _root(tmp_path)
    assert copy_group(zf, "", root, concurrency=8) == {"x": 9}
    stored = sync(root.store.get("x/c/0/0", default_buffer_prototype()))
    assert stored.to_bytes() == zf.read("x/0.0")
    np.testing.assert_array_equal(root["x"][:], data)


def test_a_failed_concurrent_write_raises(tmp_path):
    class FailingStore(MemoryStore):
        sets = 0

        async def set(self, key, value, byte_range=None):
            if not key.endswith("zarr.json"):
                self.sets += 1
                if self.sets == 3:
                    raise OSError("write failed")
            await super().set(key, value, byte_range)

    zf = _v2_zip(
        tmp_path,
        x=dict(
            data=np.arange(10, dtype="<i4"),
            chunks=(1,),
            compressors=numcodecs.Blosc(cname="lz4"),
        ),
    )
    root = zarr.group(FailingStore())
    array = create_copy(root, "x", json.loads(zf.read("x/.zarray")))
    with pytest.raises(OSError, match="write failed"):
        copy_chunks(zf, "x", array, concurrency=4)
