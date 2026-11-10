import io
import json
import zipfile

import numpy as np
import pytest

from bundle import Bundle, read_v2_array
from .stores import zip_v2_store
from sources import SAMPLES


def test_sample_urls():
    assert SAMPLES["kidney"].url("_outs.zip").endswith("/1.0.4/Atera_v1_WTA_Kidney_non_diseased_FFPE/Atera_v1_WTA_Kidney_non_diseased_FFPE_outs.zip")
    assert [s.published for s in SAMPLES.values()] == ["2026-10-15", "2026-11-03", "2026-11-05", "2026-11-05"]


def _outs_zip() -> bytes:
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("a/.zarray", "{}")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_STORED) as z:
        z.writestr("experiment.spatial", json.dumps({"pixel_size": 0.21249222}))
        z.writestr("cells.zarr.zip", inner.getvalue())
    return outer.getvalue()


def test_bundle_opens_members_locally_and_remotely(tmp_path, range_server):
    path = tmp_path / "outs.zip"
    path.write_bytes(_outs_zip())
    range_server.files["/outs.zip"] = path.read_bytes()
    for location in (path, f"{range_server.url}/outs.zip"):
        b = Bundle(location)
        assert "cells.zarr.zip" in b.names
        assert b.experiment()["pixel_size"] == 0.21249222
        assert "a/.zarray" in b.member_zip("cells.zarr.zip").namelist()


def test_read_v2_array_assembles_chunks_in_the_stored_order(tmp_path):
    fortran = np.arange(35, dtype="<f4").reshape(7, 5)
    zf = zip_v2_store(
        tmp_path,
        "store",
        {
            "g/f": (fortran, {"order": "F", "chunks": (3, 2)}),
            "g/v": (np.arange(10, dtype="<u4"), {"chunks": (4,)}),
        },
    )
    assert np.array_equal(read_v2_array(zf, "g/f"), fortran)
    assert np.array_equal(read_v2_array(zf, "g/v"), np.arange(10, dtype="<u4"))
