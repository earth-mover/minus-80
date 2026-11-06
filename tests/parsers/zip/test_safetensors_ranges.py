"""Tensor byte ranges, against files written by ``safetensors`` and zips written by ``zipfile``."""
import io
import zipfile

import numpy as np
from safetensors.numpy import load, save

from minus80.parsers.zip.buffers import walk_local_headers
from minus80.parsers.zip.safetensors_ranges import tensor_ranges

TENSORS = {
    "data": np.arange(14, dtype="<i8").reshape(7, 2),
    "sizes": np.arange(7, dtype="<i8") * 100,
    "weights": np.linspace(0, 1, 5, dtype="<f4"),
}


def test_ranges_slice_out_each_tensor_as_safetensors_loads_it():
    data = save(TENSORS, metadata={"format": "np"})
    loaded = load(data)
    ranges = tensor_ranges(data)
    assert set(ranges) == set(TENSORS)
    for name, r in ranges.items():
        assert r.shape == loaded[name].shape
        assert data[r.start:r.stop] == loaded[name].tobytes()


def test_file_offset_gives_absolute_ranges_inside_a_stored_zip_member():
    file = save(TENSORS)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("other.bin", b"x" * 1234)
        zf.writestr("cells.safetensors", file)
    archive = buf.getvalue()
    member = next(m for m in walk_local_headers(archive) if m.name == "cells.safetensors")
    for name, r in tensor_ranges(file, member.entry.data_offset).items():
        assert archive[r.start:r.stop] == TENSORS[name].tobytes()
