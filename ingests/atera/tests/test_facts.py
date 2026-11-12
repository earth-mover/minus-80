import io
import json
import struct
import zipfile

import numpy as np
import pytest
import tifffile

import facts
from bundle import Bundle, open_tiff
from facts import bundle_facts, compare, load_record, write_record
from tests.test_images import ALIGNMENT, PIXEL, _rgb_pyramid, write_bundle
from tests.test_ingest_tables import TILES
from tests.test_verify import _bundle_members

ALIGNMENT_TEXT = "\n".join(",".join(map(str, row)) for row in ALIGNMENT.tolist())


class Recorder:
    """A file that records the byte range of each read."""

    def __init__(self, path):
        self.file, self.reads = open(path, "rb"), []

    def read(self, size=-1):
        start = self.file.tell()
        data = self.file.read(size)
        self.reads.append((start, start + len(data)))
        return data

    def seek(self, offset, whence=0):
        return self.file.seek(offset, whence)

    def tell(self):
        return self.file.tell()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.file.close()


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    """The synthetic full bundle of the verify tests, and an H&E image."""
    directory = tmp_path_factory.mktemp("facts")
    path, he = directory / "outs.zip", directory / "he.ome.tif"
    write_bundle(path, _bundle_members(directory))
    _rgb_pyramid(he, planar=False)
    return path, he


@pytest.fixture(scope="module")
def found(synthetic):
    path, he = synthetic
    with open(he, "rb") as he_file:
        return bundle_facts(Bundle(path), he_file, ALIGNMENT_TEXT)


def test_bundle_facts_of_the_synthetic_bundle(found):
    assert sorted(found) == ["alignment", "cells", "diffexp", "experiment", "he_image", "members", "metrics",
                             "morphology_2d", "morphology_3d", "polygons", "qc", "transcripts"]
    assert found["morphology_3d"]["planes"] == 3 and len(found["morphology_2d"]) == 4
    assert json.loads(json.dumps(found)) == found


def test_bundle_facts_of_the_bundle_and_its_metadata(found, synthetic):
    sizes = {i.filename: i.file_size for i in zipfile.ZipFile(synthetic[0]).infolist()}
    assert found["members"] == sizes
    assert found["experiment"] == {"analysis_sw_version": "1", "pixel_size": PIXEL,
                                   "keys": ["analysis_sw_version", "instrument_sw_version", "panel_wta", "pixel_size"]}
    assert found["metrics"] == {"member": "metrics_summary_x.csv",
                                "keys": ["number_of_nucleus_non_simple_polygons", "other"]}
    assert found["alignment"] == ALIGNMENT.tolist()
    assert found["cells"] == {"count": 3, "cell_summary_columns": ["nucleus_count"]}
    assert found["diffexp"] == {f"diffexp_genes/gene_expression_{n}.csv": {"columns": 3, "first": ["Feature ID", "Feature Name"]}
                                for n in ("azimuth", "graphclust")}


def test_bundle_facts_of_the_images(found):
    stack = found["morphology_3d"]
    assert stack["member"] == "morphology_3d/ch0000_dapi_3d.ome.tif" and stack["levels"][0][0] == 3
    assert stack["tile"] == [1024, 1024] and stack["planar"] is False and stack["dtype"] == "uint16"
    assert stack["pixel_size"] == {"x": PIXEL, "y": PIXEL, "z": 2.5}
    (name, entry), *_ = found["morphology_2d"].items()
    assert name.startswith("morphology_2d/ch0000") and entry["levels"] == [[1300, 2100], [650, 1050], [325, 525]]
    assert entry["pixel_size"] == {"x": PIXEL, "y": PIXEL} and entry["compression"] == "JPEG2000"
    he = found["he_image"]
    assert he["levels"][0] == [1300, 2100, 3] and he["tile"] == [1024, 1024] and he["compression"] == "ADOBE_DEFLATE"
    assert he["planar"] is False and he["dtype"] == "uint8" and he["pixel_size"] == {}


def test_bundle_facts_of_the_transcripts_polygons_and_qc(found):
    assert found["transcripts"] == {
        "grid_keys": list(TILES), "grid_number_objects": list(TILES.values()), "number_rnas": sum(TILES.values()),
        "chunk_rows_tile": "0,0",
        "chunk_rows": {"cell_id": 2, "codeword_identity": 2, "location": 2, "overlaps_nucleus": 2, "quality_score": 2}}
    assert found["polygons"] == {"0": {"rows": 3, "unique_cells": 2, "methods": {"d": 2, "e": 1}},
                                 "1": {"rows": 3, "unique_cells": 3, "methods": {"b": 2, "c": 1}}}
    raster = found["qc"]["transcript_density"]
    assert raster == {"label": "Transcript Density Map", "dtype": "uint16", "shape": [4, 6], "bin_size": 20.0,
                      "origin": [-10.0, -20.0], "unit": "µm"}
    assert sorted(found["qc"]) == ["genomic_controls", "mean_q_score", "mean_q_score_80um",
                                   "negative_control_codewords", "negative_control_probes", "transcript_density"]


def test_compare_names_each_difference():
    assert compare({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3, "d": 4}}) == [
        "a.c: record 2, bundle 3", "a.d: record absent, bundle 4"]


def test_compare_treats_a_list_as_one_value_and_a_key_of_the_record_as_a_difference():
    assert compare({"a": [1, 2], "b": {"x": 1}, "c": "s"}, {"a": [1, 3], "c": "s"}) == [
        "a: record [1, 2], bundle [1, 3]", 'b: record {"x": 1}, bundle absent']
    assert compare({"a": {"b": [1]}}, {"a": {"b": [1]}}) == []


def test_compare_of_the_facts_with_themselves_is_empty(found):
    assert compare(found, json.loads(json.dumps(found))) == []


def test_a_record_is_written_sorted_and_loaded_without_its_sample(tmp_path, found):
    path = write_record("brain", found, tmp_path)
    assert path == tmp_path / "brain.json"
    text = path.read_text()
    assert json.loads(text)["sample"] == "brain" and text.endswith("\n")
    assert list(json.loads(text)) == sorted(json.loads(text))
    assert load_record("brain", tmp_path) == found


def test_load_record_refuses_another_sample_and_an_absent_file(tmp_path):
    (tmp_path / "brain.json").write_text(json.dumps({"sample": "kidney"}))
    with pytest.raises(ValueError, match="brain.json.*sample.*kidney"):
        load_record("brain", tmp_path)
    with pytest.raises(ValueError, match="no record.*colorectal"):
        load_record("colorectal", tmp_path)


def test_the_records_directory_is_beside_the_module():
    assert facts.RECORDS == facts.Path(facts.__file__).with_name("samples")


def _data_start(raw, info):
    name_length, extra_length = struct.unpack("<HH", raw[info.header_offset + 26:info.header_offset + 30])
    return info.header_offset + 30 + name_length + extra_length


def _tile_ranges_of_bundle(path):
    """The byte ranges of every TIFF tile of the outs.zip."""
    raw, outer, ranges = path.read_bytes(), zipfile.ZipFile(path), []
    for name in outer.namelist():
        if name.endswith(".ome.tif"):
            info = outer.getinfo(name)
            start = _data_start(raw, info)
            ranges += _tile_ranges(raw[start:start + info.file_size], start)
    return ranges


def _tile_ranges(raw, shift=0):
    with open_tiff(io.BytesIO(raw)) as tif:
        return [(shift + o, shift + o + n) for level in tif.series[0].levels for page in level.pages
                for o, n in zip(page.dataoffsets, page.databytecounts)]


def _covered(reads, start, end):
    """The bytes of ``start:end`` that some read covers."""
    clipped = sorted((max(a, start), min(b, end)) for a, b in reads if a < end and start < b)
    total, reached = 0, start
    for a, b in clipped:
        total += max(0, b - max(a, reached))
        reached = max(reached, b)
    return total


def _chunk_is_read_by_the_facts(name):
    return name.rsplit("/", 1)[-1].startswith(".") or "/cell_index/" in name or "/method/" in name


def test_bundle_facts_reads_no_pixel_no_transcript_column_no_vertex_and_no_matrix_chunk(synthetic, monkeypatch):
    path, he_path = synthetic
    opened, original = [], zipfile.ZipFile.open

    def record(self, name, *args, **kwargs):
        opened.append(getattr(name, "filename", name))
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", record)
    with Recorder(path) as bundle_file, Recorder(he_path) as he_file:
        bundle_facts(Bundle(path, bundle_file), he_file, ALIGNMENT_TEXT)
    # A zarr chunk of the inner zips opens as a zip member; only the two index arrays and metadata may open.
    assert opened and [n for n in opened if not _chunk_is_read_by_the_facts(n)] == []
    # A buffered read next to a tile header reaches into the tile, but never reads a whole tile.
    for reads, tiles in ((bundle_file.reads, _tile_ranges_of_bundle(path)), (he_file.reads, _tile_ranges(he_path.read_bytes()))):
        assert len(tiles) > 5 and reads
        covered = [_covered(reads, start, end) for start, end in tiles]
        assert max(covered) <= 8192


def test_diffexp_reads_the_first_line_only(tmp_path):
    members = _bundle_members(tmp_path)
    members["diffexp_genes/gene_expression_azimuth.csv"] = b"Feature ID,Feature Name\n" + b"ENSG1,A\n" * 100_000
    write_bundle(tmp_path / "outs.zip", members)
    he = tmp_path / "he.tif"
    _rgb_pyramid(he, planar=False)
    with Recorder(tmp_path / "outs.zip") as recorder, open(he, "rb") as he_file:
        record = bundle_facts(Bundle(tmp_path / "outs.zip", recorder), he_file, ALIGNMENT_TEXT)
    assert record["diffexp"]["diffexp_genes/gene_expression_azimuth.csv"] == {
        "columns": 2, "first": ["Feature ID", "Feature Name"]}
    assert sum(b - a for a, b in recorder.reads) < 700_000


def test_a_morphology_file_without_a_ome_xml_pixel_size_gives_an_empty_size(tmp_path):
    path = tmp_path / "t.tif"
    tifffile.imwrite(path, np.zeros((4, 4), "u1"))
    with open_tiff(path) as tif:
        assert facts.pixel_sizes(tif.pages.first) == {}
