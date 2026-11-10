import io
import json
import zipfile

from bundle import Bundle
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
