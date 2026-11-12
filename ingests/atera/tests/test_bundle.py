import io
import json
import zipfile

import pytest

from bundle import Bundle
from sources import S3, SAMPLES


def test_sample_urls():
    assert SAMPLES["kidney"].url("_outs.zip").endswith("/1.0.4/Atera_v1_WTA_Kidney_non_diseased_FFPE/Atera_v1_WTA_Kidney_non_diseased_FFPE_outs.zip")
    assert SAMPLES["kidney"].url("_outs.zip", S3).startswith("s3://10x.files/samples/atera/1.0.4/")
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


def test_open_bundle_counts_the_bytes_it_reads(tmp_path):
    from ingest import open_bundle

    path = tmp_path / "outs.zip"
    path.write_bytes(_outs_zip())
    bundle, counter = open_bundle(str(path))
    assert counter.bytes > 0
    before = counter.bytes
    bundle.experiment()
    assert counter.bytes >= before


def _bundle_of(tmp_path, names):
    path = tmp_path / "outs.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name in names:
            z.writestr(name, "")
    return Bundle(path)


def test_bundle_finds_its_morphology_metrics_and_report_by_pattern(tmp_path):
    two_d = [f"morphology_2d/ch000{i}_x.ome.tif" for i in (3, 1, 0, 2)]
    bundle = _bundle_of(tmp_path, [*two_d, "morphology_3d/ch0000_dapi_3d.ome.tif", "metrics_summary_a.csv",
                                   "analysis_summary_a.html", "morphology_2d/other.txt", "metrics_summary_a.html"])
    assert bundle.morphology_2d() == sorted(two_d)
    assert bundle.morphology_3d() == "morphology_3d/ch0000_dapi_3d.ome.tif"
    assert bundle.metrics() == "metrics_summary_a.csv"
    assert bundle.report() == "analysis_summary_a.html"


def test_morphology_members_need_their_count(tmp_path):
    bundle = _bundle_of(tmp_path, [f"morphology_2d/ch000{i}.ome.tif" for i in range(3)])
    with pytest.raises(ValueError, match=r"morphology_2d/ch\*\.ome\.tif.*3"):
        bundle.morphology_2d()
    with pytest.raises(ValueError, match=r"morphology_3d/ch\*\.ome\.tif.*none"):
        bundle.morphology_3d()


def test_metrics_and_report_need_exactly_one_member(tmp_path):
    bundle = _bundle_of(tmp_path, ["metrics_summary_a.csv", "metrics_summary_b.csv", "analysis_summary_a.html",
                                   "analysis_summary_b.html"])
    with pytest.raises(ValueError, match=r"metrics_summary.*\.csv.*metrics_summary_a\.csv.*metrics_summary_b\.csv"):
        bundle.metrics()
    with pytest.raises(ValueError, match=r"analysis_summary.*\.html.*analysis_summary_b\.html"):
        bundle.report()
