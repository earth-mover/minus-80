import io
import json
import re
import struct
import zipfile
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pytest
import tifffile

import facts
from bundle import CELLS, MATRIX, TRANSCRIPTS, Bundle, open_tiff
from facts import ByteLimit, STORE_MEMBERS, bundle_facts, compare, error_paths, error_text, load_record, write_record
from minus80.core.registry import as_url
from minus80.core.remote import counting_reader
from tests.stores import cells_zip
from tests.test_images import ALIGNMENT, PIXEL, _rgb_pyramid, write_bundle
from tests.test_ingest_tables import TILES
from tests.test_qc import DENSITY, _bag, _html, _resource
from tests.test_verify import _bundle_members

BYTES_CEILING = 600_000
ALIGNMENT_TEXT = "\n".join(",".join(map(str, row)) for row in ALIGNMENT.tolist())
#: A home directory's root, joined at run time so that the leak check finds no home path in this file.
USERS = "/".join(["", "Users"])


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

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.file.close()


class Synthetic(NamedTuple):
    path: Path
    he: Path
    members: dict


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    """The synthetic full bundle of the verify tests, its members, and an H&E image."""
    directory = tmp_path_factory.mktemp("facts")
    members = _bundle_members(directory)
    path, he = directory / "outs.zip", directory / "he.ome.tif"
    write_bundle(path, members)
    _rgb_pyramid(he, planar=False)
    return Synthetic(path, he, members)


def _facts_of(directory, members, he_path, alignment=ALIGNMENT_TEXT):
    """``bundle_facts`` of the bundle of ``members``, with the H&E file and the alignment text given."""
    write_bundle(directory / "variant.zip", members)
    he_bytes = he_path.read_bytes()
    return bundle_facts(Bundle(directory / "variant.zip"), lambda: io.BytesIO(he_bytes), lambda: alignment)


@pytest.fixture(scope="module")
def found(synthetic):
    return _facts_of(synthetic.path.parent, synthetic.members, synthetic.he)


def _variant(tmp_path, synthetic, change, **kwargs):
    members = dict(synthetic.members)
    change(members)
    return _facts_of(tmp_path, members, kwargs.get("he", synthetic.he))


def test_bundle_facts_of_the_synthetic_bundle(found):
    assert sorted(found) == ["alignment", "cells", "diffexp", "experiment", "he_image", "members", "metrics",
                             "morphology_2d", "morphology_3d", "polygons", "qc", "report", "stores", "transcripts"]
    assert found["morphology_3d"]["files"]["morphology_3d/ch0000_dapi_3d.ome.tif"]["planes"] == 3
    assert found["morphology_2d"]["count"] == 4 and len(found["morphology_2d"]["files"]) == 4
    assert error_paths(found) == []
    assert json.loads(json.dumps(found)) == found


def test_bundle_facts_of_the_bundle_and_its_metadata(found, synthetic):
    sizes = {i.filename: i.file_size for i in zipfile.ZipFile(synthetic.path).infolist()}
    assert found["members"] == sizes
    assert found["experiment"] == {"analysis_sw_version": "1", "pixel_size": PIXEL,
                                   "keys": ["analysis_sw_version", "instrument_sw_version", "panel_wta", "pixel_size"]}
    assert found["metrics"] == {"count": 1, "files": {
        "metrics_summary_x.csv": ["number_of_nucleus_non_simple_polygons", "other"]}}
    assert found["report"] == {"count": 1, "files": ["analysis_summary_x.html"]}
    assert found["alignment"] == ALIGNMENT.tolist()
    assert found["cells"] == {"count": 3, "cell_summary_columns": ["nucleus_count"]}
    assert found["diffexp"] == {f"diffexp_genes/gene_expression_{n}.csv": {"columns": 3, "first": ["Feature ID", "Feature Name"]}
                                for n in ("azimuth", "graphclust")}


def test_bundle_facts_of_the_images(found):
    (name, stack), = found["morphology_3d"]["files"].items()
    assert name == "morphology_3d/ch0000_dapi_3d.ome.tif" and stack["levels"][0][0] == 3
    assert stack["tile"] == [1024, 1024] and stack["planar"] is False and stack["dtype"] == "uint16"
    assert stack["pixel_size"] == {"x": PIXEL, "y": PIXEL, "z": 2.5}
    (name, entry), *_ = found["morphology_2d"]["files"].items()
    assert name.startswith("morphology_2d/ch0000") and entry["levels"] == [[1300, 2100], [650, 1050], [325, 525]]
    assert entry["pixel_size"] == {"x": PIXEL, "y": PIXEL} and entry["planes"] == 1
    assert entry["compression"] == {"code": 34712, "name": "JPEG2000"}
    assert entry["series"] == 1 and entry["pages"] >= 1 and entry["photometric"] == 1 and entry["planarconfig"] == 1
    he = found["he_image"]
    assert he["levels"][0] == [1300, 2100, 3] and he["tile"] == [1024, 1024] and he["photometric"] == 2
    assert he["compression"]["name"] == "ADOBE_DEFLATE" and he["planarconfig"] == 1
    assert he["planar"] is False and he["dtype"] == "uint8" and he["pixel_size"] == {}


def test_bundle_facts_of_the_transcripts_polygons_and_cells(found):
    assert found["transcripts"] == {
        "grid_keys": list(TILES), "grid_number_objects": list(TILES.values()), "number_rnas": sum(TILES.values()),
        "first_tile": {"key": "0,0", "chunk_rows": {
            "cell_id": 2, "codeword_identity": 2, "location": 2, "overlaps_nucleus": 2, "quality_score": 2}}}
    assert found["polygons"] == {
        "set_indices": ["0", "1"], "method_names": ["a", "b", "c", "d", "e"],
        "sets": {"0": {"rows": 3, "unique_cells": 2, "methods": {"3:d": 2, "4:e": 1}},
                 "1": {"rows": 3, "unique_cells": 3, "methods": {"1:b": 2, "2:c": 1}}}}


def test_bundle_facts_of_the_qc_report_has_the_ingests_view_and_the_raw_view(found):
    qc = found["qc"]
    assert qc["member"] == "analysis_summary_x.html"
    assert qc["rasters"]["transcript_density"] == {
        "label": "Transcript Density Map", "dtype": "uint16", "shape": [4, 6], "bin_size": 20.0,
        "origin": [-10.0, -20.0], "unit": "µm"}
    assert sorted(qc["rasters"]) == ["genomic_controls", "mean_q_score", "mean_q_score_80um",
                                     "negative_control_codewords", "negative_control_probes", "transcript_density"]
    assert qc["raw"]["unit"] == "µm" and "Genomic Controls" in qc["raw"]["bins"]
    assert qc["raw"]["resources"]["density_plot_transcript_density_map"]["values"] == 24


def test_bundle_facts_of_the_zarr_stores(found):
    cells = found["stores"][CELLS]
    assert cells["arrays"]["masks/0"] == {"dtype": "<u4", "shape": [6, 8], "chunks": [3, 4], "compressor": None}
    assert cells["arrays"]["cell_summary"]["attrs"] == ["column_names"]
    assert cells["arrays"]["polygon_sets/1/cell_index"]["shape"] == [3] and cells["groups"][""] == ["segmentation_methods"]
    transcripts = found["stores"][TRANSCRIPTS]
    assert transcripts["tiles"] == {"count": 3, "listed": "0,0", "directories_equal_grid_keys": True}
    assert {p for p in transcripts["arrays"] if p.startswith("grid/")} == {
        f"grid/0,0/{c}" for c in ("cell_id", "codeword_identity", "location", "overlaps_nucleus", "quality_score")}
    assert transcripts["groups"]["grid"] == ["grid_keys", "grid_number_objects"]
    assert transcripts["groups"][""] == ["codeword_gene_mapping", "codeword_gene_names", "number_rnas"]
    assert found["stores"][MATRIX]["arrays"] and "tiles" not in cells


def test_compare_names_each_difference():
    assert compare({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3, "d": 4}}) == [
        "a.c: record 2, bundle 3", "a.d: record absent, bundle 4"]


def test_compare_treats_a_list_as_one_value_and_a_key_of_the_record_as_a_difference():
    assert compare({"a": [1, 2], "b": {"x": 1}, "c": "s"}, {"a": [1, 3], "c": "s"}) == [
        "a: record [1, 2], bundle [1, 3]", 'b: record {"x": 1}, bundle absent']
    assert compare({"a": {"b": [1]}}, {"a": {"b": [1]}}) == []


def test_compare_of_the_facts_with_themselves_is_empty(found):
    assert compare(found, json.loads(json.dumps(found))) == []


def test_an_error_entry_in_the_bundle_is_a_difference_from_a_clean_record():
    assert compare({"a": {"x": 1}}, {"a": {"error": "ValueError: no"}}) == [
        "a.error: record absent, bundle \"ValueError: no\"", "a.x: record 1, bundle absent"]


def test_a_record_is_written_sorted_and_loaded_without_its_sample(tmp_path, found):
    assert write_record("brain", found, tmp_path) == []
    text = (tmp_path / "brain.json").read_text()
    assert json.loads(text)["sample"] == "brain" and text.endswith("\n")
    assert list(json.loads(text)) == sorted(json.loads(text))
    assert load_record("brain", tmp_path) == found


def test_write_record_writes_error_entries_and_returns_their_paths_and_load_record_refuses_them(tmp_path):
    found = {"he_image": {"error": "OSError: x"}, "a": {"b": {"error": "KeyError: 'k'"}, "c": 1}}
    assert write_record("brain", found, tmp_path) == ["a.b", "he_image"]
    assert json.loads((tmp_path / "brain.json").read_text())["he_image"] == {"error": "OSError: x"}
    with pytest.raises(ValueError, match=r"brain\.json.*a\.b.*he_image"):
        load_record("brain", tmp_path)


def test_load_record_refuses_another_sample_and_an_absent_file(tmp_path):
    (tmp_path / "brain.json").write_text(json.dumps({"sample": "kidney"}))
    with pytest.raises(ValueError, match="brain.json.*sample.*kidney"):
        load_record("brain", tmp_path)
    with pytest.raises(ValueError, match="no record.*colorectal"):
        load_record("colorectal", tmp_path)


def test_load_record_refuses_a_file_that_is_not_json_with_its_base_name(tmp_path):
    (tmp_path / "brain.json").write_text("{not json")
    with pytest.raises(ValueError, match=r"^brain\.json is not JSON$"):
        load_record("brain", tmp_path)


def test_the_records_directory_is_beside_the_module():
    assert facts.RECORDS == facts.Path(facts.__file__).with_name("samples")


def test_an_error_entry_keeps_the_type_and_the_first_line_and_no_location():
    error = FileNotFoundError(
        f"s3://10x.files/samples/atera/x_outs.zip and {USERS}/someone/data/x.zip, '/tmp/y z' (10x.files/z)\nline /home/a")
    text = error_text(error)
    assert text.startswith("FileNotFoundError: ") and "and" in text
    assert text == "FileNotFoundError: <location> and <location>"
    assert error_text(ValueError("no morphology_2d/ch0000.ome.tif")) == "ValueError: no morphology_2d/ch0000.ome.tif"


def test_the_record_holds_no_url_and_no_local_path(found, synthetic):
    text = json.dumps(found)
    assert "://" not in text and str(synthetic.path.parent) not in text and "/Users" not in text


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
    opened, original = [], zipfile.ZipFile.open

    def record(self, name, *args, **kwargs):
        opened.append(getattr(name, "filename", name))
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", record)
    with Recorder(synthetic.path) as bundle_file, Recorder(synthetic.he) as he_file:
        bundle_facts(Bundle(synthetic.path, bundle_file), lambda: he_file, lambda: ALIGNMENT_TEXT)
    # A zarr chunk of the inner zips opens as a zip member; only the two index arrays and metadata may open.
    assert opened and [n for n in opened if not _chunk_is_read_by_the_facts(n)] == []
    # A buffered read next to a tile header reaches into the tile, but never reads a whole tile.
    for reads, tiles in ((bundle_file.reads, _tile_ranges_of_bundle(synthetic.path)),
                         (he_file.reads, _tile_ranges(synthetic.he.read_bytes()))):
        assert len(tiles) > 5 and reads
        covered = [_covered(reads, start, end) for start, end in tiles]
        assert max(covered) <= 8192
    # The bundle is 14 MB, mostly tiles; the facts, with the stores' hierarchy, read a small part of it.
    assert sum(b - a for a, b in bundle_file.reads) < BYTES_CEILING


def test_diffexp_reads_the_first_line_only(tmp_path, synthetic):
    members = dict(synthetic.members)
    members["diffexp_genes/gene_expression_azimuth.csv"] = b"Feature ID,Feature Name\n" + b"ENSG1,A\n" * 100_000
    write_bundle(tmp_path / "outs.zip", members)
    with Recorder(tmp_path / "outs.zip") as recorder:
        record = bundle_facts(Bundle(tmp_path / "outs.zip", recorder), lambda: io.BytesIO(synthetic.he.read_bytes()),
                              lambda: ALIGNMENT_TEXT)
    assert record["diffexp"]["diffexp_genes/gene_expression_azimuth.csv"] == {
        "columns": 2, "first": ["Feature ID", "Feature Name"]}
    assert sum(b - a for a, b in recorder.reads) < 700_000


def test_a_tiff_without_ome_xml_gives_an_empty_pixel_size(tmp_path):
    path = tmp_path / "t.tif"
    tifffile.imwrite(path, np.zeros((4, 4), "u1"))
    with open_tiff(path) as tif:
        assert facts.pixel_sizes(tif.pages.first) == {}


# A bundle that differs from the kidney's layout: each fact that can still be read is in the record.

def test_three_morphology_2d_files_are_recorded_with_their_count_and_no_error(tmp_path, synthetic):
    gone = sorted(n for n in synthetic.members if n.startswith("morphology_2d/"))[0]
    found = _variant(tmp_path, synthetic, lambda m: m.pop(gone))
    assert found["morphology_2d"]["count"] == 3 and gone not in found["morphology_2d"]["files"]
    assert error_paths(found) == []


def test_a_bad_tiff_hides_no_other_file(tmp_path, synthetic):
    bad = sorted(n for n in synthetic.members if n.startswith("morphology_2d/"))[1]
    found = _variant(tmp_path, synthetic, lambda m: m.__setitem__(bad, b"not a tiff"))
    assert list(found["morphology_2d"]["files"][bad]) == ["error"]
    assert sum("levels" in e for e in found["morphology_2d"]["files"].values()) == 3
    assert error_paths(found) == [f"morphology_2d.files.{bad}"]
    assert "morphology_3d" in found and "levels" in found["he_image"]


def test_no_stack_and_two_reports_and_two_metrics_members_are_recorded(tmp_path, synthetic):
    def change(members):
        del members["morphology_3d/ch0000_dapi_3d.ome.tif"]
        members["analysis_summary_y.html"] = members["analysis_summary_x.html"]
        members["metrics_summary_y.csv"] = members["metrics_summary_x.csv"]

    found = _variant(tmp_path, synthetic, change)
    assert found["morphology_3d"] == {"count": 0, "files": {}}
    assert found["report"] == {"count": 2, "files": ["analysis_summary_x.html", "analysis_summary_y.html"]}
    assert sorted(found["metrics"]["files"]) == ["metrics_summary_x.csv", "metrics_summary_y.csv"]
    assert found["metrics"]["count"] == 2 and found["qc"]["member"] == "analysis_summary_x.html"
    assert error_paths(found) == []


def _report(change):
    bag = _bag()
    bag["density_plot"]["bins"] |= {
        label: f"_resources_{key}" for label, key in (
            ("Negative Control Codewords", "codewords"), ("Negative Control Probes", "probes"),
            ("Genomic Controls", "genomic"))}
    bag["_resources"] |= {key: _resource(DENSITY) for key in ("codewords", "probes", "genomic")}
    change(bag)
    return _html(bag).encode()


def test_an_unknown_qc_label_fails_the_ingests_view_and_stays_in_the_raw_view(tmp_path, synthetic):
    def change(bag):
        bag["density_plot"]["bins"]["Weird Map"] = "_resources_weird"
        bag["_resources"]["weird"] = _resource(DENSITY)

    found = _variant(tmp_path, synthetic, lambda m: m.__setitem__("analysis_summary_x.html", _report(change)))
    assert found["qc"]["rasters"]["error"].startswith("ValueError: unknown QC raster label 'Weird Map'")
    assert found["qc"]["raw"]["bins"]["Weird Map"] == "_resources_weird"
    assert found["qc"]["raw"]["resources"]["weird"]["values"] == 24
    assert error_paths(found) == ["qc.rasters"]


def test_a_unit_other_than_micrometre_fails_the_ingests_view_and_is_in_the_raw_view(tmp_path, synthetic):
    def change(bag):
        bag["density_plot"]["unit"] = "mm"

    found = _variant(tmp_path, synthetic, lambda m: m.__setitem__("analysis_summary_x.html", _report(change)))
    assert "unit is 'mm'" in found["qc"]["rasters"]["error"] and found["qc"]["raw"]["unit"] == "mm"


SQUARE = [0, 0, 1, 0, 1, 1, 0, 1, 0, 0]


def _cells_member(tmp_path, extra):
    tmp_path.mkdir(exist_ok=True)
    summary = (np.array([[1.0], [0.0], [2.0]]), {"attributes": {"column_names": ["nucleus_count"]}})
    zf = cells_zip(tmp_path, extra={"cell_summary": summary, **extra}, attrs={"segmentation_methods": list("abcde")})
    return Path(zf.filename).read_bytes()


def test_an_unknown_method_code_is_recorded_with_a_question_mark(tmp_path, synthetic):
    extra = {"polygon_sets/0/method": (np.array([3, 3, 9], dtype="<u4"), {})}
    found = _variant(tmp_path, synthetic,
                     lambda m: m.__setitem__("cells.zarr.zip", _cells_member(tmp_path / "c", extra)))
    assert found["polygons"]["sets"]["0"]["methods"] == {"3:d": 2, "9:?": 1}
    assert error_paths(found) == []


def test_three_polygon_sets_are_recorded(tmp_path, synthetic):
    extra = {"polygon_sets/2/vertices": (np.array([SQUARE], dtype="<f4"), {}),
             "polygon_sets/2/num_vertices": (np.array([4], dtype="<i4"), {}),
             "polygon_sets/2/cell_index": (np.array([1], dtype="<u4"), {}),
             "polygon_sets/2/method": (np.array([0], dtype="<u4"), {})}
    found = _variant(tmp_path, synthetic,
                     lambda m: m.__setitem__("cells.zarr.zip", _cells_member(tmp_path / "c", extra)))
    assert found["polygons"]["set_indices"] == ["0", "1", "2"]
    assert found["polygons"]["sets"]["2"] == {"rows": 1, "unique_cells": 1, "methods": {"0:a": 1}}


def test_a_planar_he_image_is_recorded_as_planar(tmp_path, synthetic):
    he = tmp_path / "planar.tif"
    _rgb_pyramid(he, planar=True)
    found = _variant(tmp_path, synthetic, lambda m: None, he=he)
    assert found["he_image"]["planar"] is True and found["he_image"]["planarconfig"] == 2
    assert found["he_image"]["levels"][0] == [3, 1300, 2100] and found["he_image"]["compression"]["name"] == "NONE"


def test_a_missing_experiment_key_is_recorded_as_absent_and_does_not_fail_the_group(tmp_path, synthetic):
    found = _variant(tmp_path, synthetic,
                     lambda m: m.__setitem__("experiment.spatial", json.dumps({"panel_wta": {}}).encode()))
    assert found["experiment"] == {"keys": ["panel_wta"]}
    assert error_paths(found) == []


def test_a_missing_he_object_is_an_error_entry_and_the_other_groups_remain(tmp_path, synthetic):
    write_bundle(tmp_path / "outs.zip", synthetic.members)

    def missing():
        raise FileNotFoundError("s3://10x.files/samples/atera/x_he_image.ome.tif is absent from /tmp/cache")

    found = bundle_facts(Bundle(tmp_path / "outs.zip"), missing, lambda: ALIGNMENT_TEXT)
    assert found["he_image"] == {"error": "FileNotFoundError: <location> is absent from <location>"}
    assert error_paths(found) == ["he_image"] and found["alignment"] == ALIGNMENT.tolist()
    assert found["transcripts"]["number_rnas"] == sum(TILES.values())


def test_a_missing_alignment_object_is_an_error_entry_and_the_other_groups_remain(tmp_path, synthetic):
    def missing():
        raise OSError("no alignment")

    write_bundle(tmp_path / "outs.zip", synthetic.members)
    found = bundle_facts(Bundle(tmp_path / "outs.zip"), lambda: io.BytesIO(synthetic.he.read_bytes()), missing)
    assert found["alignment"] == {"error": "OSError: no alignment"} and error_paths(found) == ["alignment"]
    assert "levels" in found["he_image"]


def test_an_absolute_path_with_a_space_leaks_no_tail():
    assert error_text(OSError(f"cannot open '{USERS}/a/b c/x.zip' now")) == "OSError: cannot open <location>"


def test_the_he_file_is_closed_after_the_facts_and_after_a_failed_open(tmp_path, synthetic):
    write_bundle(tmp_path / "outs.zip", synthetic.members)
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"not a tiff")
    for path, readable in ((synthetic.he, True), (bad, False)):
        handle = open(path, "rb")
        found = bundle_facts(Bundle(tmp_path / "outs.zip"), lambda: handle, lambda: ALIGNMENT_TEXT)
        assert handle.closed and ("levels" in found["he_image"]) is readable


def test_a_negative_method_code_is_recorded_with_a_question_mark(tmp_path, synthetic):
    extra = {"polygon_sets/0/method": (np.array([3, -1, 4], dtype="<i4"), {})}
    found = _variant(tmp_path, synthetic,
                     lambda m: m.__setitem__("cells.zarr.zip", _cells_member(tmp_path / "c", extra)))
    assert found["polygons"]["sets"]["0"]["methods"] == {"3:d": 1, "-1:?": 1, "4:e": 1}


# The byte ceiling: the groups read before it stay, the group in progress and the later ones are error entries.

class _Limit:
    """The progress hook that the survey's ceiling is."""

    def __init__(self, limit, total):
        self.limit, self.total = limit, total

    def advance(self, size):
        self.total += size
        if self.total > self.limit:
            raise ByteLimit(f"read {self.total} bytes, over the limit of {self.limit} bytes")


def _facts_with_limit(synthetic, limit=None):
    file, counter = counting_reader(as_url(synthetic.path))
    bundle = Bundle(synthetic.path, file)
    if limit is not None:
        counter.progress = _Limit(limit, counter.bytes)
    he_bytes = synthetic.he.read_bytes()
    found = bundle_facts(bundle, lambda: io.BytesIO(he_bytes), lambda: ALIGNMENT_TEXT)
    return found, counter.bytes


GROUP_ORDER = ["members", "experiment", "metrics", "report", "alignment", "cells", "transcripts", "diffexp", "polygons",
               "qc", "morphology_2d", "morphology_3d", "he_image", f"stores.{CELLS}", f"stores.{TRANSCRIPTS}",
               f"stores.{MATRIX}"]


def _entry(record, path):
    group, _, store = path.partition(".")
    return record[group][store] if store else record[group]


def test_a_ceiling_passed_in_a_read_makes_the_group_and_every_later_group_an_error_entry(synthetic):
    full, total = _facts_with_limit(synthetic)
    partial, _ = _facts_with_limit(synthetic, total - 1)
    errors = error_paths(partial)
    assert errors and errors == sorted(GROUP_ORDER[-len(errors):])
    for path in errors:
        assert re.fullmatch(r"ByteLimit: read \d+ bytes, over the limit of \d+ bytes", _entry(partial, path)["error"])
    for path in GROUP_ORDER[:-len(errors)]:
        assert _entry(partial, path) == _entry(full, path)


def test_a_ceiling_passed_in_the_matrix_store_keeps_every_earlier_group(synthetic, monkeypatch):
    original = facts.store_facts

    def passing(bundle, member, **kwargs):
        found = original(bundle, member, **kwargs)
        if member == MATRIX:
            raise ByteLimit("read 9 bytes, over the limit of 1 bytes")
        return found

    monkeypatch.setattr(facts, "store_facts", passing)
    partial, _ = _facts_with_limit(synthetic)
    monkeypatch.undo()
    full, _ = _facts_with_limit(synthetic)
    assert error_paths(partial) == [f"stores.{MATRIX}"]
    assert partial["stores"][MATRIX] == {"error": "ByteLimit: read 9 bytes, over the limit of 1 bytes"}
    assert all(_entry(partial, p) == _entry(full, p) for p in GROUP_ORDER[:-1])


def test_a_ceiling_passed_in_an_early_group_gives_error_entries_from_there_on(synthetic, monkeypatch):
    def passing(bundle):
        raise ByteLimit("read 9 bytes, over the limit of 1 bytes")

    monkeypatch.setattr(facts, "metrics_facts", passing)
    partial, _ = _facts_with_limit(synthetic)
    assert partial["members"] and "keys" in partial["experiment"]
    assert error_paths(partial) == sorted(p for p in GROUP_ORDER if p not in ("members", "experiment"))


# The listing of a store.

META = {"dtype": "<f4", "shape": [1, 3], "chunks": [1, 3], "compressor": None, "fill_value": 0, "filters": None,
        "order": "C", "zarr_format": 2}


def _store_bundle(tmp_path, members_of_store, store=TRANSCRIPTS):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_STORED) as zf:
        for name, text in members_of_store.items():
            zf.writestr(name, text)
    write_bundle(tmp_path / "s.zip", {store: inner.getvalue()})
    return Bundle(tmp_path / "s.zip")


def _grid(directories, keys=None, counts=None):
    members = {f"grid/{d}/{c}/.zarray": json.dumps(META) for d in directories for c in ("cell_id", "location")}
    if keys is not None:
        members["grid/.zattrs"] = json.dumps({"grid_keys": keys, "grid_number_objects": counts})
    return members


def _opened(monkeypatch):
    names, original = [], zipfile.ZipFile.open

    def record(self, name, *args, **kwargs):
        names.append(getattr(name, "filename", name))
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", record)
    return names


def test_a_store_whose_tile_directories_differ_from_the_grid_keys_lists_one_tile(tmp_path, monkeypatch):
    bundle = _store_bundle(tmp_path, _grid(["0,0", "1,0", "-1,0"], ["a", "b", "c"], [1, 1, 1]))
    opened = _opened(monkeypatch)
    found = facts.store_facts(bundle, TRANSCRIPTS, first_tile_only=True)
    assert found["tiles"] == {"count": 3, "listed": "-1,0", "directories_equal_grid_keys": False}
    assert sorted(found["arrays"]) == ["grid/-1,0/cell_id", "grid/-1,0/location"]
    assert {n.split("/")[1] for n in opened if n.startswith("grid/") and n.endswith(".zarray")} == {"-1,0"}


def test_a_store_lists_the_first_tile_with_rows_when_the_keys_match_its_directories(tmp_path):
    bundle = _store_bundle(tmp_path, _grid(["0,0", "1,0"], ["0,0", "1,0"], [0, 5]))
    found = facts.store_facts(bundle, TRANSCRIPTS, first_tile_only=True)
    assert found["tiles"] == {"count": 2, "listed": "1,0", "directories_equal_grid_keys": True}
    assert sorted(found["arrays"]) == ["grid/1,0/cell_id", "grid/1,0/location"]


def test_a_store_with_unreadable_grid_attributes_still_lists_its_arrays(tmp_path):
    bundle = _store_bundle(tmp_path, _grid(["0,0", "1,0"]))
    found = facts.store_facts(bundle, TRANSCRIPTS, first_tile_only=True)
    assert found["tiles"] == {"count": 2, "listed": "0,0", "directories_equal_grid_keys": False}
    assert sorted(found["arrays"]) == ["grid/0,0/cell_id", "grid/0,0/location"] and "error" not in found


def test_a_store_listing_stops_at_the_member_cap(tmp_path, monkeypatch):
    bundle = _store_bundle(tmp_path, {f"a{i}/.zarray": json.dumps(META) for i in range(STORE_MEMBERS + 2000)}, MATRIX)
    opened = _opened(monkeypatch)
    found = facts.store_facts(bundle, MATRIX)
    assert STORE_MEMBERS == 3000 and len(found["arrays"]) == 3000 and len(opened) == 3000
    assert found["truncated"] is True and found["members_not_read"] == 2000
