import numpy as np
import pytest
import zarr
from virtualizarr.manifests import ManifestGroup, ManifestStore

from minus80.core.builders.flat_binary import flat_manifest_array
from minus80.core.registry import as_url, registry_for

SHAPE = (4, 3, 2)


def _read(marr) -> np.ndarray:
    url = next(marr.manifest.iter_nonempty_paths())
    store = ManifestStore(ManifestGroup(arrays={"a": marr}), registry=registry_for(url))
    return zarr.open_group(store, mode="r")["a"][:]


def _volume(dtype) -> np.ndarray:
    return np.arange(np.prod(SHAPE), dtype=dtype).reshape(SHAPE) * 257


@pytest.mark.parametrize(("dtype", "endian"), [("<u2", "little"), (">u2", "big")])
def test_contiguous_volume_after_a_header(tmp_path, dtype, endian):
    data = _volume(dtype)
    path = tmp_path / "volume.raw"
    path.write_bytes(b"H" * 24 + data.tobytes())
    marr = flat_manifest_array(as_url(path), shape=SHAPE, dtype="uint16", data_offset=24,
                               endian=endian, dimension_names=("z", "y", "x"))
    np.testing.assert_array_equal(_read(marr), np.fromfile(path, dtype=dtype, offset=24).reshape(SHAPE))


def test_stride_skips_a_header_before_each_slab(tmp_path):
    data = _volume("<u2")
    slab = data[0].nbytes
    path = tmp_path / "records.raw"
    path.write_bytes(b"".join(b"R" * 16 + s.tobytes() for s in data))
    marr = flat_manifest_array(as_url(path), shape=SHAPE, dtype="uint16", data_offset=16,
                               endian="little", stride=16 + slab)
    raw = np.frombuffer(path.read_bytes(), dtype=np.uint8).reshape(SHAPE[0], 16 + slab)[:, 16:]
    np.testing.assert_array_equal(_read(marr), raw.copy().view("<u2").reshape(SHAPE))


def test_stride_smaller_than_a_slab_raises(tmp_path):
    with pytest.raises(ValueError, match="overlap"):
        flat_manifest_array("file:///x.raw", shape=SHAPE, dtype="uint16", data_offset=0,
                            endian="little", stride=4)
