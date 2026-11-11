"""The QC rasters of the analysis summary: decoded from a synthetic report, written, and verified."""

import base64
import json
import zipfile
import zlib

import numpy as np
import pytest
import zarr

import ingest
from bundle import Bundle
from ingest import write_qc
from minus80.core.icechunk_io import commit
from qc import rasters
from tests.test_images import ALIGNMENT, PIXEL, _main, _repo, _rgb_pyramid, bundle, writer  # noqa: F401
from verify import gate_qc

TAG = '<script id="ws-data" type="application/json">'
DENSITY = np.arange(24, dtype="<u2").reshape(4, 6) * 7
QUALITY = (np.arange(24, dtype="<f4").reshape(4, 6) / 3).astype("<f4")
COARSE = np.array([[1.5, 40.0]], dtype="<f4")


def _resource(array):
    payload = base64.b64encode(zlib.compress(np.ascontiguousarray(array).tobytes())).decode()
    return f"data:{array.dtype.name}/zip;base64,{payload}"


def _bag():
    bag = {
        "density_plot": {
            "bins": {"Transcript Density Map": "_resources_density_plot_transcript_density_map",
                     "Mean Q-Score": "_resources_density_plot_mean_q_score"},
            "mean_q_score_80um": "_resources_density_plot_mean_q_score_80um",
            "dimensions": {"height": 70.0, "width": 120.0},
            "horizontal_bin_count": 6,
            "origin_data": [-10.0, -20.0],
            "unit": "µm",
            "large_image": "data:image/jpeg;base64,AAAA",
        },
        "_resources": {
            "density_plot_transcript_density_map": _resource(DENSITY),
            "density_plot_mean_q_score": _resource(QUALITY),
            "density_plot_mean_q_score_80um": _resource(COARSE),
        },
    }
    return bag


def _html(bag):
    return f"<html><!-- {TAG} not json --><body>{TAG}\n{json.dumps(bag)}</script></body></html>"


def test_rasters_have_their_values_dtype_shape_and_grid():
    found = rasters(_html(_bag()))
    assert sorted(found) == ["mean_q_score", "mean_q_score_80um", "transcript_density"]
    density = found["transcript_density"]
    np.testing.assert_array_equal(density.array, DENSITY)
    assert density.array.dtype == np.uint16 and density.array.shape == (4, 6)
    assert (density.name, density.label, density.unit) == ("transcript_density", "Transcript Density Map", "µm")
    assert density.bin_size == 20.0 and density.origin == (-10.0, -20.0)
    quality = found["mean_q_score"]
    np.testing.assert_array_equal(quality.array, QUALITY)
    assert quality.array.dtype == np.float32 and quality.label == "Mean Q-Score"


def test_the_coarse_map_takes_its_bin_size_from_its_key_and_its_shape_from_the_ceil():
    coarse = rasters(_html(_bag()))["mean_q_score_80um"]
    assert coarse.bin_size == 80.0 and coarse.array.shape == (1, 2) and coarse.origin == (-10.0, -20.0)
    np.testing.assert_array_equal(coarse.array, COARSE)


def test_a_label_the_table_lacks_raises():
    bag = _bag()
    bag["density_plot"]["bins"]["Mean Mystery"] = "_resources_density_plot_mean_q_score"
    with pytest.raises(ValueError, match="Mean Mystery"):
        rasters(_html(bag))


def test_a_wrong_value_count_raises():
    bag = _bag()
    bag["_resources"]["density_plot_mean_q_score"] = _resource(QUALITY[:3])
    with pytest.raises(ValueError, match="values"):
        rasters(_html(bag))


def test_a_wrong_coarse_value_count_raises():
    bag = _bag()
    bag["_resources"]["density_plot_mean_q_score_80um"] = _resource(np.zeros(3, dtype="<f4"))
    with pytest.raises(ValueError, match="values"):
        rasters(_html(bag))


def test_an_unknown_dtype_raises_with_the_header():
    bag = _bag()
    bag["_resources"]["density_plot_mean_q_score"] = _resource(QUALITY.astype("<f8"))
    with pytest.raises(ValueError, match="data:float64/zip;base64"):
        rasters(_html(bag))


def test_a_report_without_the_data_bag_raises():
    with pytest.raises(ValueError, match="ws-data"):
        rasters("<html><body>nothing</body></html>")


@pytest.fixture
def qc_bundle(bundle, tmp_path):  # noqa: F811
    """The synthetic bundle with an analysis summary and the report that the ingest ignores."""
    with zipfile.ZipFile(tmp_path / "outs.zip", "a", zipfile.ZIP_STORED) as zf:
        zf.writestr("analysis_summary_RnD_S2_kidney_ND.html", _html(_bag()))
        zf.writestr("qc_images.html", "<html></html>")
    return Bundle(tmp_path / "outs.zip")


def test_the_report_is_the_one_member_that_starts_with_analysis_summary(bundle, qc_bundle, tmp_path):  # noqa: F811
    from qc import bundle_rasters

    assert sorted(bundle_rasters(qc_bundle)) == ["mean_q_score", "mean_q_score_80um", "transcript_density"]
    with pytest.raises(ValueError, match="analysis_summary"):
        bundle_rasters(bundle)
    with zipfile.ZipFile(tmp_path / "outs.zip", "a") as zf:
        zf.writestr("analysis_summary_other.html", "")
    with pytest.raises(ValueError, match="analysis_summary"):
        bundle_rasters(Bundle(tmp_path / "outs.zip"))


def _root(writer):  # noqa: F811
    return zarr.open_group(writer.repo.readonly_session(branch="work").store, mode="r")


def _apply(transform, point):
    """Apply a sequence of scale and translation steps to ``point``, a list in the order of the axes."""
    point = np.array(point, dtype=float)
    for step in transform["transformations"]:
        point = point * np.array(step["scale"]) if step["type"] == "scale" else point + np.array(step["translation"])
    return point


def test_write_qc_stores_each_raster_as_one_chunk_with_its_grid(qc_bundle, writer):  # noqa: F811
    write_qc(writer, qc_bundle)
    root = _root(writer)
    assert sorted(root["images"].group_keys()) == ["qc_mean_q_score", "qc_mean_q_score_80um", "qc_transcript_density"]
    for name, expected, label in (("transcript_density", DENSITY, "Transcript Density Map"),
                                  ("mean_q_score", QUALITY, "Mean Q-Score")):
        group = root[f"images/qc_{name}"]
        array = group["s0"]
        assert array.shape == (1, 4, 6) and array.chunks == (1, 4, 6) and array.dtype == expected.dtype
        np.testing.assert_array_equal(array[0], expected)
        assert group.attrs["minus80:complete"] is True
        ome = group.attrs["ome"]
        assert [c["label"] for c in ome["omero"]["channels"]] == [label]
        transform = ome["multiscales"][0]["coordinateTransformations"][0]
        assert [s["type"] for s in transform["transformations"]] == ["scale", "translation"]
        assert transform["transformations"][0]["scale"] == [1.0, 20.0, 20.0]
        assert transform["transformations"][1]["translation"] == [0.0, -20.0, -10.0]
        assert transform["transformations"][0]["output"] == transform["transformations"][1]["input"]
        # Pixel (0, 0) is the corner of bin 0; its far corner is the corner of the next bin.
        np.testing.assert_allclose(_apply(transform, [0, 0, 0]), [0, -20.0, -10.0])
        np.testing.assert_allclose(_apply(transform, [0, 1, 1]), [0, 0.0, 10.0])
    coarse = root["images/qc_mean_q_score_80um"]
    assert coarse["s0"].shape == (1, 1, 2) and coarse["s0"].dtype == np.float32
    assert coarse.attrs["ome"]["multiscales"][0]["coordinateTransformations"][0]["transformations"][0]["scale"] == [1.0, 80.0, 80.0]


def test_write_qc_refuses_a_raster_the_report_lacks(qc_bundle, writer):  # noqa: F811
    with pytest.raises(ValueError, match="negative_control_probes"):
        write_qc(writer, qc_bundle, ["negative_control_probes"])
    assert not writer.has("images/qc_negative_control_probes")


def test_a_rerun_of_write_qc_skips_held_elements(qc_bundle, writer, capsys):  # noqa: F811
    write_qc(writer, qc_bundle)
    capsys.readouterr()
    write_qc(writer, qc_bundle)
    out = capsys.readouterr().out
    assert out.count("already, skipped") == 3 and "commit" not in out


def test_full_mode_writes_the_qc_rasters_and_does_not_list_them_as_missing(monkeypatch, tmp_path, qc_bundle):  # noqa: F811
    assert not any("qc_" in m["what"] for m in ingest.MISSING_FROM_FULL)
    assert any("overview_scan" in m["what"] for m in ingest.MISSING_FROM_FULL)
    he, alignment = tmp_path / "he.ome.tif", tmp_path / "alignment.csv"
    _rgb_pyramid(he, planar=False)
    np.savetxt(alignment, ALIGNMENT, delimiter=",")
    monkeypatch.chdir(tmp_path)
    _main(monkeypatch, tmp_path, "--mode", "full", "--branch", "full", "--bundle", str(tmp_path / "outs.zip"),
          "--he-image", str(he), "--he-alignment", str(alignment),
          "--elements", "images/qc_transcript_density,images/qc_mean_q_score_80um")
    from minus80.core.icechunk_io import create_repo

    root = zarr.open_group(create_repo(tmp_path / "repo", [], open_existing=True).readonly_session(branch="full").store,
                           mode="r")
    assert sorted(root["images"].group_keys()) == ["qc_mean_q_score_80um", "qc_transcript_density"]


@pytest.fixture
def qc_store(qc_bundle, writer):  # noqa: F811
    write_qc(writer, qc_bundle)
    return writer.repo


def test_gate_qc_passes_on_a_store_written_in_full(qc_bundle, qc_store):
    assert gate_qc(zarr.open_group(qc_store.readonly_session(branch="work").store, mode="r"), qc_bundle) == []


def test_gate_qc_passes_on_a_store_without_qc_elements(qc_bundle, writer):  # noqa: F811
    session = writer.repo.writable_session("work")
    zarr.open_group(session.store, mode="a").require_group("images")
    commit(session, "an empty images group")
    assert gate_qc(_root(writer), qc_bundle) == []


def _corrupt(repo, change):
    session = repo.writable_session("work")
    change(zarr.open_group(session.store, mode="a"))
    commit(session, "corrupt")
    return zarr.open_group(repo.readonly_session(branch="work").store, mode="r")


def test_gate_qc_fails_when_one_value_differs(qc_bundle, qc_store):
    def change(root):
        root["images/qc_mean_q_score/s0"][0, 1, 2] += 1

    errors = gate_qc(_corrupt(qc_store, change), qc_bundle)
    assert len(errors) == 1 and "qc_mean_q_score" in errors[0] and "values" in errors[0]


def test_gate_qc_fails_when_the_translation_differs(qc_bundle, qc_store):
    def change(root):
        attrs = root["images/qc_transcript_density"].attrs.asdict()
        attrs["ome"]["multiscales"][0]["coordinateTransformations"][0]["transformations"][1]["translation"][2] = 0.0
        root["images/qc_transcript_density"].attrs.put(attrs)

    errors = gate_qc(_corrupt(qc_store, change), qc_bundle)
    assert len(errors) == 1 and "qc_transcript_density" in errors[0] and "translation" in errors[0]


def test_gate_qc_fails_when_the_scale_differs(qc_bundle, qc_store):
    def change(root):
        attrs = root["images/qc_mean_q_score"].attrs.asdict()
        attrs["ome"]["multiscales"][0]["coordinateTransformations"][0]["transformations"][0]["scale"][1] = 21.0
        root["images/qc_mean_q_score"].attrs.put(attrs)

    errors = gate_qc(_corrupt(qc_store, change), qc_bundle)
    assert len(errors) == 1 and "scale" in errors[0]
