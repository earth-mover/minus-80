import numpy as np
import pytest
import zarr
from virtualizarr.manifests import ManifestGroup, ManifestStore

from minus80.core.manifest import MISSING, channel_last_transpose, manifest_array
from minus80.core.registry import as_url, registry_for

BYTES = [{"name": "bytes", "configuration": {"endian": "little"}}]


def _read(marr) -> np.ndarray:
    url = next(marr.manifest.iter_nonempty_paths())
    store = ManifestStore(ManifestGroup(arrays={"a": marr}), registry=registry_for(url))
    return zarr.open_group(store, mode="r")["a"][:]


def _entries(marr):
    return {idx: (e["path"], e["offset"], e["length"]) for idx, e in marr.manifest.dict().items()}


def test_zero_length_chunk_is_missing_with_offset_zero():
    marr = manifest_array("file:///a.bin", [[8, 16]], [[4, 0]], shape=(1, 4), chunk_shape=(1, 2),
                          dtype="uint16", codecs=BYTES)
    assert _entries(marr) == {"0.0": ("file:///a.bin", 8, 4)}


@pytest.mark.parametrize("absent", [None, MISSING])
def test_absent_path_is_missing(absent):
    paths = np.array([["file:///a.bin", absent]], dtype=object)
    marr = manifest_array(paths, [[8, 16]], [[4, 4]], shape=(1, 4), chunk_shape=(1, 2),
                          dtype="uint16", codecs=BYTES)
    assert _entries(marr) == {"0.0": ("file:///a.bin", 8, 4)}


def test_offsets_not_shaped_like_the_grid_raise():
    with pytest.raises(ValueError, match="chunk grid"):
        manifest_array("file:///a.bin", [0, 4], [4, 4], shape=(1, 4), chunk_shape=(1, 2),
                       dtype="uint16", codecs=BYTES)


def test_source_url_is_a_string_for_one_file():
    marr = manifest_array("file:///a.bin", [[0, 4]], [[4, 4]], shape=(1, 4), chunk_shape=(1, 2),
                          dtype="uint16", codecs=BYTES, attributes={"source_url": "stale"})
    assert marr.metadata.attributes["source_url"] == "file:///a.bin"


def test_source_url_lists_every_referenced_file_but_not_missing_chunks():
    paths = np.array([["file:///b.bin", "file:///a.bin", "file:///c.bin"]], dtype=object)
    marr = manifest_array(paths, [[0, 0, 0]], [[4, 4, 0]], shape=(1, 6), chunk_shape=(1, 2),
                          dtype="uint16", codecs=BYTES)
    assert marr.metadata.attributes["source_url"] == ["file:///a.bin", "file:///b.bin"]


def test_nan_attribute_becomes_null():
    marr = manifest_array("file:///a.bin", [[0]], [[4]], shape=(1, 2), chunk_shape=(1, 2),
                          dtype="uint16", codecs=BYTES, attributes={"gain": float("nan")})
    assert marr.metadata.attributes["gain"] is None


def test_scale_for_an_unknown_dimension_raises():
    with pytest.raises(ValueError, match="not dimensions"):
        manifest_array("file:///a.bin", [[0]], [[4]], shape=(1, 2), chunk_shape=(1, 2),
                       dtype="uint16", codecs=BYTES, dimension_names=("y", "x"),
                       attributes={"scale": {"z": 1.0}})


@pytest.mark.parametrize("leading", [(), (2,)])
def test_channel_last_transpose_reads_channel_last_bytes_as_channel_first(tmp_path, leading):
    stored = np.arange(np.prod((*leading, 4, 5, 3)), dtype=np.uint16).reshape(*leading, 4, 5, 3)
    path = tmp_path / "frames.bin"
    stored.tofile(path)
    rank = stored.ndim
    expected = np.moveaxis(stored, -1, -3)
    frame = stored[(0,) * len(leading)].nbytes
    offsets = (np.arange(int(np.prod(leading))) * frame).reshape(*leading, 1, 1, 1)
    marr = manifest_array(
        as_url(path), offsets, np.full(offsets.shape, frame),
        shape=expected.shape, chunk_shape=(*(1,) * len(leading), 3, 4, 5), dtype="uint16",
        codecs=[channel_last_transpose(rank), *BYTES])
    np.testing.assert_array_equal(_read(marr), expected)


def test_channel_last_transpose_orders():
    assert channel_last_transpose(3)["configuration"]["order"] == [1, 2, 0]
    assert channel_last_transpose(4)["configuration"]["order"] == [0, 2, 3, 1]
