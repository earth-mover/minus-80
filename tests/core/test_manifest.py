import numpy as np
import pytest
import zarr
from virtualizarr.manifests import ManifestGroup, ManifestStore

from minus80.core.icechunk_io import create_repo
from minus80.core.manifest import (
    MISSING,
    channel_last_transpose,
    concatenate,
    manifest_array,
    permute_dims,
    stack,
)
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


def test_chunk_past_the_end_of_the_object_raises_naming_it():
    with pytest.raises(ValueError, match=r"chunk at \(0, 1\) ends at byte 9, past the end of the 8-byte"):
        manifest_array("file:///a.bin", [[0, 4]], [[4, 5]], shape=(1, 4), chunk_shape=(1, 2),
                       dtype="uint16", codecs=BYTES, size=8)


def test_chunks_within_the_object_and_missing_chunks_pass_the_size_check():
    marr = manifest_array("file:///a.bin", [[0, 400]], [[8, 0]], shape=(1, 4), chunk_shape=(1, 2),
                          dtype="uint16", codecs=BYTES, size=8)
    assert _entries(marr) == {"0.0": ("file:///a.bin", 0, 8)}


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


def _typed(dimensions):
    return manifest_array("file:///a.bin", [[0]], [[4]], shape=(1, 2), chunk_shape=(1, 2),
                          dtype="uint16", codecs=BYTES, dimension_names=("y", "x"),
                          attributes={"dimensions": dimensions})


def test_dimensions_keep_each_axis_type():
    marr = _typed({"y": {"type": "space"}, "x": {"type": "space", "note": "columns"}})
    assert marr.metadata.attributes["dimensions"]["x"] == {"type": "space", "note": "columns"}


def test_dimensions_for_an_unknown_dimension_raise():
    with pytest.raises(ValueError, match="not dimensions"):
        _typed({"t": {"type": "time"}})


@pytest.mark.parametrize("axis", [{}, {"type": None}, "space"])
def test_dimensions_entry_without_a_type_string_raises(axis):
    with pytest.raises(TypeError, match="'type' string"):
        _typed({"y": axis})


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


ZCYX = {"z": {"type": "space"}, "c": {"type": "channel"}, "y": {"type": "space"},
        "x": {"type": "space"}}


def _volume(tmp_path, name, data, *, chunks, attributes=None, codecs=BYTES, stored=None):
    """``data`` written to a file one chunk after another, and the array referencing it.

    ``stored`` is the axis order each chunk's bytes are written in, when not
    the array's own.
    """
    path = tmp_path / name
    grid = tuple(s // c for s, c in zip(data.shape, chunks))
    offsets = np.zeros(grid, dtype=np.uint64)
    with open(path, "wb") as f:
        for index in np.ndindex(grid):
            block = data[tuple(slice(i * c, (i + 1) * c) for i, c in zip(index, chunks))]
            offsets[index] = f.tell()
            f.write(np.ascontiguousarray(block.transpose(stored) if stored else block).tobytes())
    return manifest_array(as_url(path), offsets, np.full(grid, np.prod(chunks) * data.itemsize),
                          shape=data.shape, chunk_shape=chunks, dtype=data.dtype, codecs=codecs,
                          dimension_names=tuple("zcyx"[-data.ndim:]),
                          attributes={"dimensions": dict(list(ZCYX.items())[-data.ndim:]),
                                      **(attributes or {})})


def _through_icechunk(tmp_path, marr) -> np.ndarray:
    urls = marr.metadata.attributes["source_url"]
    repo = create_repo(tmp_path / "repo", [urls] if isinstance(urls, str) else urls)
    session = repo.writable_session("main")
    ManifestGroup(arrays={"a": marr}).to_icechunk(session.store)
    session.commit("write")
    return zarr.open_group(repo.readonly_session("main").store, mode="r")["a"][:]


def _zcyx(seed=0):
    return (np.arange(3 * 2 * 4 * 5, dtype=np.uint16) + seed).reshape(3, 2, 4, 5)


def test_stack_names_the_axis_and_lists_every_source(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5), attributes={"scale": {"z": 0.5}})
    b = _volume(tmp_path, "b.bin", _zcyx(100), chunks=(1, 1, 4, 5), attributes={"scale": {"z": 0.5}})
    fovs = stack([a, b], dim="fov", axis={"type": "fov"})
    assert fovs.metadata.dimension_names == ("fov", "z", "c", "y", "x")
    attrs = fovs.metadata.attributes
    assert attrs["source_url"] == sorted([as_url(tmp_path / "a.bin"), as_url(tmp_path / "b.bin")])
    assert list(attrs["dimensions"]) == ["fov", "z", "c", "y", "x"]
    assert attrs["dimensions"]["fov"] == {"type": "fov"}
    assert attrs["scale"] == {"z": 0.5}
    np.testing.assert_array_equal(_through_icechunk(tmp_path, fovs), np.stack([_zcyx(), _zcyx(100)]))


def test_stack_at_a_position(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5))
    b = _volume(tmp_path, "b.bin", _zcyx(1), chunks=(1, 1, 4, 5))
    assert stack([a, b], dim="t", position=1).metadata.dimension_names == ("z", "t", "c", "y", "x")
    assert stack([a, b], dim="t", position=-3).metadata.dimension_names == ("z", "c", "t", "y", "x")


def test_stack_refuses_arrays_whose_attributes_differ(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5), attributes={"scale": {"z": 0.5}})
    b = _volume(tmp_path, "b.bin", _zcyx(), chunks=(1, 1, 4, 5), attributes={"scale": {"z": 0.2}})
    with pytest.raises(ValueError, match="scale"):
        stack([a, b], dim="fov")


def test_stack_refuses_an_existing_dimension_name(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5))
    with pytest.raises(ValueError, match="already a dimension"):
        stack([a, a], dim="z")


def test_stack_refuses_an_axis_without_a_type(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5))
    with pytest.raises(TypeError, match="'type' string"):
        stack([a, a], dim="fov", axis={"kind": "fov"})


def test_concatenate_along_a_named_axis(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5))
    b = _volume(tmp_path, "b.bin", _zcyx(100), chunks=(1, 1, 4, 5))
    joined = concatenate([a, b], dim="z")
    assert joined.shape == (6, 2, 4, 5)
    assert joined.metadata.dimension_names == ("z", "c", "y", "x")
    assert len(joined.metadata.attributes["source_url"]) == 2
    np.testing.assert_array_equal(_through_icechunk(tmp_path, joined),
                                  np.concatenate([_zcyx(), _zcyx(100)]))


def test_concatenate_refuses_differing_attributes_and_unknown_axes(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5), attributes={"gain": 1})
    b = _volume(tmp_path, "b.bin", _zcyx(), chunks=(1, 1, 4, 5), attributes={"gain": 2})
    with pytest.raises(ValueError, match="gain"):
        concatenate([a, b], dim="z")
    with pytest.raises(ValueError, match="not a dimension"):
        concatenate([a, a], dim="t")


def test_permute_dims_of_one_plane_per_chunk_keeps_the_codecs(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5), attributes={"scale": {"z": 0.5, "y": 0.2}})
    czyx = permute_dims(a, ("c", "z", "y", "x"))
    assert czyx.shape == (2, 3, 4, 5)
    assert czyx.metadata.dimension_names == ("c", "z", "y", "x")
    assert [c.to_dict()["name"] for c in czyx.metadata.codecs] == ["bytes"]
    assert list(czyx.metadata.attributes["dimensions"]) == ["c", "z", "y", "x"]
    assert czyx.manifest.dict()["1.2.0.0"] == a.manifest.dict()["2.1.0.0"]
    np.testing.assert_array_equal(_through_icechunk(tmp_path, czyx), _zcyx().transpose(1, 0, 2, 3))


def test_permute_dims_inside_a_chunk_adds_a_transpose(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 2, 4, 5))
    zcxy = permute_dims(a, (0, 1, 3, 2))
    first = zcxy.metadata.codecs[0].to_dict()
    assert first["name"] == "transpose"
    assert list(first["configuration"]["order"]) == [0, 1, 3, 2]
    np.testing.assert_array_equal(_through_icechunk(tmp_path, zcxy), _zcyx().transpose(0, 1, 3, 2))


@pytest.mark.parametrize("order, codec", [
    (("y", "x", "c"), None),
    (("x", "y", "c"), [1, 0, 2]),
    (("c", "x", "y"), [2, 1, 0]),
])
def test_permute_dims_merges_an_existing_transpose(tmp_path, order, codec):
    cyx = np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5)
    a = _volume(tmp_path, "a.bin", cyx, chunks=(3, 4, 5), stored=(1, 2, 0),
                codecs=[channel_last_transpose(3), *BYTES])
    np.testing.assert_array_equal(_through_icechunk(tmp_path / "before", a), cyx)
    permuted = permute_dims(a, order)
    codecs = [c.to_dict() for c in permuted.metadata.codecs]
    transposes = [list(c["configuration"]["order"]) for c in codecs if c["name"] == "transpose"]
    assert transposes == ([] if codec is None else [codec])
    want = cyx.transpose(["cyx".index(o) for o in order])
    np.testing.assert_array_equal(_through_icechunk(tmp_path / "after", permuted), want)


def test_permute_dims_refuses_a_partial_order(tmp_path):
    a = _volume(tmp_path, "a.bin", _zcyx(), chunks=(1, 1, 4, 5))
    with pytest.raises(ValueError, match="not a permutation"):
        permute_dims(a, ("c", "z", "y"))
