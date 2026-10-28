"""Chunks of a Zarr v2 store decoded from their keys' bytes, checked against zarr reading the same store."""

import numcodecs
import numpy as np
import pytest
import zarr
from zarr.storage import LocalStore

from minus80.parsers.zip.zarr_v2 import (
    ArraySpec,
    ChunkKey,
    array_paths,
    chunk_key,
    decode_chunk,
    frame_problem,
    is_metadata,
)


def _store(path, separator):
    values = np.arange(4 * 25 * 30, dtype="<u2").reshape(4, 25, 30)
    group = zarr.open_group(LocalStore(path), mode="w", zarr_format=2)
    array = group.create_array(
        "w/0", shape=values.shape, chunks=(1, 10, 16), dtype=values.dtype,
        compressors=numcodecs.Blosc(cname="lz4", clevel=5, shuffle=1),
        chunk_key_encoding={"name": "v2", "separator": separator},
    )
    array[:] = values
    files = {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}
    return values, files


@pytest.mark.parametrize("separator", ["/", "."])
def test_every_chunk_decodes_to_zarrs_values(tmp_path, separator):
    values, files = _store(tmp_path, separator)
    arrays = array_paths(files)
    assert arrays == {"w/0"}
    spec = ArraySpec.from_document(files["w/0/.zarray"])
    assert spec.grid() == (4, 3, 2)
    decoded = 0
    for name, data in files.items():
        if is_metadata(name):
            assert chunk_key(name, arrays) is None
            continue
        key = chunk_key(name, arrays)
        assert key is not None and key.array == "w/0"
        c, y, x = key.index
        chunk = decode_chunk(data, spec)
        expected = values[c:c + 1, y * 10:y * 10 + 10, x * 16:x * 16 + 16]
        np.testing.assert_array_equal(chunk[: expected.shape[0], : expected.shape[1], : expected.shape[2]], expected)
        decoded += 1
    assert decoded == 4 * 3 * 2


def test_names_that_are_not_chunks():
    arrays = {"a/0", "a"}
    assert chunk_key("a/0/1/2", arrays) == ChunkKey("a/0", (1, 2))
    assert chunk_key("a/0/.zarray", arrays) is None
    assert chunk_key("a/tables/x/0", arrays) is None
    assert chunk_key("b/1/2", arrays) is None


def test_encodings_compare_without_shape():
    base = {"zarr_format": 2, "shape": [4, 10], "chunks": [1, 10], "dtype": "<u2",
            "compressor": {"id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1, "blocksize": 0},
            "filters": None, "fill_value": 0, "order": "C"}
    a = ArraySpec.from_document(base)
    assert a.same_encoding(ArraySpec.from_document({**base, "shape": [8, 99]}))
    assert not a.same_encoding(ArraySpec.from_document({**base, "dtype": "<u4"}))


def test_a_blosc_frame_cut_short_shows_without_decoding():
    spec = ArraySpec.from_document({"zarr_format": 2, "shape": [100], "chunks": [100], "dtype": "<u4",
                                    "compressor": {"id": "blosc", "cname": "lz4", "clevel": 5, "shuffle": 1},
                                    "filters": None, "fill_value": 0, "order": "C"})
    frame = numcodecs.Blosc(cname="lz4", clevel=5, shuffle=1).encode(np.arange(100, dtype="<u4"))
    assert frame_problem(frame, spec.compressor) is None
    np.testing.assert_array_equal(decode_chunk(frame, spec), np.arange(100, dtype="<u4"))
    assert "truncated" in frame_problem(frame[: len(frame) // 2], spec.compressor)
