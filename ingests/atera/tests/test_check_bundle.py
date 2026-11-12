"""The bundle check, and the facts that the ingest reads when a bundle opens."""

import io
import json
import re
import sys

import pytest
import zarr

import check_bundle
import ingest
from bundle import CELLS, Bundle
from facts import bundle_facts, load_record, write_record
from ingest import MISSING_FROM_FULL, prepare, write_root
from minus80.core.elements import ElementWriter
from minus80.core.icechunk_io import create_repo, open_repository
from minus80.core.tables import local_catalog
from qc import QC_NAMES
from sources import SAMPLES
from tests.test_facts import ALIGNMENT_TEXT, Recorder, synthetic  # noqa: F401
from tests.test_images import MORPHOLOGY_2D, PIXEL, write_bundle
from tests.test_replace_tables import _preflight
from tests.test_verify import Store, full_store  # noqa: F401

STAIN = MORPHOLOGY_2D[1]


@pytest.fixture(autouse=True)
def anonymous_s3(monkeypatch):
    monkeypatch.setenv("MINUS80_S3_ANONYMOUS", "1")


def _facts(bundle_path, he_path):
    he = he_path.read_bytes()
    return bundle_facts(Bundle(bundle_path), lambda: io.BytesIO(he), lambda: ALIGNMENT_TEXT)


@pytest.fixture(scope="module")
def pristine(synthetic):  # noqa: F811
    return _facts(synthetic.path, synthetic.he)


@pytest.fixture
def alignment_file(tmp_path):
    path = tmp_path / "alignment.csv"
    path.write_text(ALIGNMENT_TEXT)
    return path


def _variant(tmp_path, synthetic, change):  # noqa: F811
    members = dict(synthetic.members)
    change(members)
    path = tmp_path / "variant.zip"
    write_bundle(path, members)
    return path


def _other_stain(members):
    members["morphology_2d/ch0001_other_stain.ome.tif"] = members.pop(STAIN)


def _check(synthetic, alignment_file, record, *args, bundle=None):  # noqa: F811
    return check_bundle.main(["--sample", "kidney", "--bundle", str(bundle or synthetic.path),
                              "--he-image", str(synthetic.he), "--he-alignment", str(alignment_file),
                              "--record", str(record), *args])


def _read(out):
    """The bytes and the requests that the last line of ``out`` reports."""
    match = re.search(r"^read (\d+) bytes in (\d+) requests", out, re.MULTILINE)
    assert match, out
    return int(match[1]), int(match[2])


def test_a_bundle_equal_to_its_record_has_no_difference(capsys, tmp_path, synthetic, alignment_file, pristine):  # noqa: F811
    write_record("kidney", pristine, tmp_path)
    assert _check(synthetic, alignment_file, tmp_path / "kidney.json") == 0
    out = capsys.readouterr().out
    assert "bundle check: 0 differences" in out.splitlines() and _read(out)[0] > 0


def test_a_stain_with_another_file_name_is_a_difference_that_gives_both_names(
        capsys, tmp_path, synthetic, alignment_file, pristine):  # noqa: F811
    write_record("kidney", pristine, tmp_path)
    variant = _variant(tmp_path, synthetic, _other_stain)
    assert _check(synthetic, alignment_file, tmp_path / "kidney.json", bundle=variant) == 1
    out = capsys.readouterr().out
    assert f"morphology_2d.files.{STAIN}: record" in out
    assert "morphology_2d.files.morphology_2d/ch0001_other_stain.ome.tif: record absent" in out
    assert re.search(r"^bundle check: [1-9]\d* differences$", out, re.MULTILINE)


def _refusal(capsys, code):
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert len(captured.err.splitlines()) == 1 and "Traceback" not in captured.err
    return captured.err


def test_a_record_with_an_error_entry_is_refused_in_one_line(capsys, tmp_path, synthetic, alignment_file, pristine):  # noqa: F811
    write_record("kidney", {**pristine, "qc": {**pristine["qc"], "rasters": {"error": "ValueError: x"}}}, tmp_path)
    err = _refusal(capsys, _check(synthetic, alignment_file, tmp_path / "kidney.json"))
    assert "qc.rasters" in err


def test_an_absent_record_is_refused_in_one_line(capsys, tmp_path, synthetic, alignment_file):  # noqa: F811
    err = _refusal(capsys, _check(synthetic, alignment_file, tmp_path / "kidney.json"))
    assert "no record of kidney" in err


def test_a_record_of_another_sample_is_refused_in_one_line(capsys, tmp_path, synthetic, alignment_file, pristine):  # noqa: F811
    write_record("brain", pristine, tmp_path)
    err = _refusal(capsys, _check(synthetic, alignment_file, tmp_path / "brain.json"))
    assert "'brain'" in err


def test_a_group_that_does_not_exist_is_refused_in_one_line(capsys, tmp_path, synthetic, alignment_file, pristine):  # noqa: F811
    write_record("kidney", pristine, tmp_path)
    err = _refusal(capsys, _check(synthetic, alignment_file, tmp_path / "kidney.json", "--skip-groups", "store"))
    assert "store" in err


def test_skipping_the_stores_leaves_them_out_of_the_comparison(
        capsys, tmp_path, synthetic, alignment_file, pristine):  # noqa: F811
    changed = json.loads(json.dumps(pristine))
    changed["stores"][CELLS]["arrays"]["masks/0"]["shape"] = [1, 1]
    write_record("kidney", changed, tmp_path)
    record = tmp_path / "kidney.json"
    assert _check(synthetic, alignment_file, record) == 1
    full = capsys.readouterr().out
    assert "stores" in full
    assert _check(synthetic, alignment_file, record, "--skip-groups", "stores") == 0
    skipped = capsys.readouterr().out
    assert "bundle check: 0 differences (not compared: stores)" in skipped.splitlines()
    assert skipped.count("stores") == 1


def test_a_record_that_is_not_json_is_refused_in_one_line(capsys, tmp_path, synthetic, alignment_file):  # noqa: F811
    (tmp_path / "kidney.json").write_text("not json")
    err = _refusal(capsys, _check(synthetic, alignment_file, tmp_path / "kidney.json"))
    assert "kidney.json is not JSON" in err and str(tmp_path) not in err


def _unreadable(tmp_path, text):
    path = tmp_path / "outs.zip"
    if text is not None:
        path.write_text(text)
    return path


@pytest.mark.parametrize("text", [None, "not a zip"], ids=["an absent path", "a text file"])
def test_a_bundle_that_does_not_open_has_its_own_exit_code_and_one_line_with_the_error_type(
        capsys, tmp_path, synthetic, alignment_file, pristine, text):  # noqa: F811
    write_record("kidney", pristine, tmp_path)
    code = _check(synthetic, alignment_file, tmp_path / "kidney.json", bundle=_unreadable(tmp_path, text))
    captured = capsys.readouterr()
    assert code == 3 and captured.out == ""
    assert len(captured.err.splitlines()) == 1 and "Traceback" not in captured.err
    assert re.fullmatch(r"the bundle does not open \(\w+\)\n", captured.err) and str(tmp_path) not in captured.err


def _bytes_read(synthetic, pristine, skip):  # noqa: F811
    """The bytes that ``check`` reads from the bundle file, which the tiny synthetic bundle's block cache hides from a counter."""
    he = synthetic.he.read_bytes()
    with Recorder(synthetic.path) as file:
        check_bundle.check(Bundle(synthetic.path, file), lambda: io.BytesIO(he), lambda: ALIGNMENT_TEXT, "kidney",
                           record=pristine, skip=skip)
    return sum(end - start for start, end in file.reads)


def test_skipping_the_stores_reads_fewer_bytes(synthetic, pristine):  # noqa: F811
    assert _bytes_read(synthetic, pristine, ("stores",)) < _bytes_read(synthetic, pristine, ())


def test_the_byte_limit_turns_the_groups_not_read_into_differences(capsys, tmp_path, synthetic, alignment_file, pristine):  # noqa: F811
    write_record("kidney", pristine, tmp_path)
    assert _check(synthetic, alignment_file, tmp_path / "kidney.json", "--max-bytes", "1") == 1
    assert "ByteLimit" in capsys.readouterr().out


def test_a_check_that_raises_has_its_own_exit_code_and_one_line_with_the_error_type(
        capsys, tmp_path, monkeypatch, synthetic, alignment_file, pristine):  # noqa: F811
    def fail(*args, **kwargs):
        raise OSError("s3://secret-bucket/prefix")

    write_record("kidney", pristine, tmp_path)
    monkeypatch.setattr(check_bundle, "check", fail)
    code = _check(synthetic, alignment_file, tmp_path / "kidney.json")
    captured = capsys.readouterr()
    assert code == 4 and captured.out == ""
    assert captured.err == "the check did not finish (OSError)\n"


def test_the_docstring_gives_the_exit_code_of_a_check_that_did_not_finish():
    assert "4 for a check that did not finish" in " ".join(check_bundle.__doc__.split())


# The check when the ingest opens the bundle

LAYOUTS = ingest.LAYOUTS.read_text()


def _inputs(tmp_path, name, change=None):
    store = Store(tmp_path / name)
    store.directory.mkdir()
    store.write_inputs(change)
    return store


def _record_of(store):
    return load_record("kidney", store.record.parent)


def _open_he(store):
    return lambda: store.he.open("rb")


def _prepare(store, record=None):
    return prepare(Bundle(store.bundle), SAMPLES["kidney"], LAYOUTS, _open_he(store), store.alignment.read_text,
                   record=record or _record_of(store))


def _drop_metric(members):
    members["metrics_summary_x.csv"] = b"number_of_nucleus_non_simple_polygons\n5\n"


def _drop_experiment_key(members):
    experiment = json.loads(members["experiment.spatial"])
    del experiment["panel_wta"]
    members["experiment.spatial"] = json.dumps(experiment).encode()


def _unknown_label(members):
    members["analysis_summary_x.html"] = members["analysis_summary_x.html"].replace(b"Genomic Controls", b"Odd Controls")


def test_prepare_gives_each_fact_that_a_later_step_reads(tmp_path, capsys):
    store = _inputs(tmp_path, "inputs")
    prepared = _prepare(store)
    assert "bundle check: 0 differences (not compared: stores)" in capsys.readouterr().out.splitlines()
    assert prepared.experiment["pixel_size"] == PIXEL
    assert prepared.methods == ["a", "b", "c", "d", "e"]
    assert set(prepared.qc) == set(QC_NAMES)
    assert prepared.root["title"] == SAMPLES["kidney"].title and prepared.root["missing"] == MISSING_FROM_FULL
    assert "fixture" not in prepared.root


def test_write_root_writes_exactly_the_root_attributes_that_prepare_built(tmp_path):
    prepared = _prepare(_inputs(tmp_path, "inputs"))
    repo = create_repo(tmp_path / "repo", [])
    repo.create_branch("work", repo.lookup_branch("main"))
    write_root(ElementWriter(repo, "work"), prepared.root)
    stored = dict(zarr.open_group(repo.readonly_session(branch="work").store, mode="r").attrs)
    assert stored == json.loads(json.dumps(prepared.root))


def test_prepare_refuses_a_bundle_that_differs_from_its_record_with_the_difference_lines(tmp_path):
    inputs = _inputs(tmp_path, "inputs")
    other = _inputs(tmp_path, "other", _other_stain)
    with pytest.raises(ValueError) as refusal:
        _prepare(other, _record_of(inputs))
    lines = str(refusal.value).splitlines()
    assert any(f"morphology_2d.files.{STAIN}: record" in line for line in lines)
    assert all(": record " in line for line in lines)


def test_prepare_compares_every_group_except_the_stores(tmp_path):
    inputs = _inputs(tmp_path, "inputs")
    record = _record_of(inputs)
    record["stores"][CELLS]["arrays"]["masks/0"]["shape"] = [1, 1]
    assert _prepare(inputs, record).experiment["pixel_size"] == PIXEL
    record["cells"]["count"] = 99
    with pytest.raises(ValueError, match=r"cells\.count: record 99, bundle 3"):
        _prepare(inputs, record)


def test_prepare_refuses_a_qc_label_that_the_ingest_does_not_know_when_the_record_agrees(tmp_path):
    odd = _inputs(tmp_path, "odd", _unknown_label)
    record = _facts(odd.bundle, odd.he)
    assert record["qc"]["rasters"]["error"]
    with pytest.raises(ValueError, match="unknown QC raster label 'Odd Controls'"):
        _prepare(odd, record)


@pytest.mark.parametrize("key", ["instrument_sw_version", "pixel_size"])
def test_prepare_refuses_an_experiment_key_that_the_ingest_needs_when_the_record_agrees(tmp_path, key):
    def drop(members):
        experiment = json.loads(members["experiment.spatial"])
        del experiment[key]
        members["experiment.spatial"] = json.dumps(experiment).encode()

    broken = _inputs(tmp_path, "broken", drop)
    with pytest.raises(ValueError, match=f"experiment.spatial has no key '{key}', which the ingest needs"):
        _prepare(broken, _facts(broken.bundle, broken.he))


def _state(store):
    """The tables' metadata files, the snapshot of each branch, and each file of the repo and the catalog."""
    catalog = local_catalog(store.catalog)
    tables = {str(t): catalog.load_table(t).metadata_location for t in catalog.list_tables("ns")}
    repo = open_repository(store.repo)
    branches = {branch: repo.lookup_branch(branch) for branch in sorted(repo.list_branches())}
    files = sorted(str(p.relative_to(store.directory)) for d in (store.repo, store.catalog) for p in d.rglob("*")
                   if p.is_file())
    return tables, branches, files


def _ingest(monkeypatch, work, inputs, *args):
    argv = ["ingest.py", "--sample", "kidney", "--repo", str(work.repo), "--catalog", str(work.catalog),
            "--namespace", "ns", "--mode", "full", "--branch", "trial", "--bundle", str(inputs.bundle),
            "--he-image", str(inputs.he), "--he-alignment", str(inputs.alignment), "--record", str(inputs.record),
            "--concurrency", "2", *args]
    monkeypatch.chdir(work.directory)
    monkeypatch.setattr(sys, "argv", argv)
    ingest.main()


CHANGES = {
    "another stain": (_other_stain, "morphology_2d.files"),
    "a metrics key lacking": (_drop_metric, "metrics.files"),
    "an experiment key lacking": (_drop_experiment_key, "experiment.keys"),
    "an unknown qc label": (_unknown_label, "qc"),
}


@pytest.mark.parametrize("change, named", CHANGES.values(), ids=CHANGES)
def test_a_bundle_that_differs_from_its_record_stops_a_run_before_any_catalog_or_repo_exists(
        monkeypatch, tmp_path, change, named):
    record = _inputs(tmp_path, "record")
    other = _inputs(tmp_path, "other", change)
    work = Store(tmp_path / "work")
    work.directory.mkdir()
    other.record = record.record
    with pytest.raises(SystemExit, match=named):
        _ingest(monkeypatch, work, other)
    assert not work.repo.exists() and not work.catalog.exists()


@pytest.mark.parametrize("change, named", CHANGES.values(), ids=CHANGES)
def test_a_bundle_that_differs_from_its_record_leaves_a_written_store_as_it_was(
        monkeypatch, tmp_path, full_store, change, named):  # noqa: F811
    record = _inputs(tmp_path, "record")
    other = _inputs(tmp_path, "other", change)
    other.record = record.record
    work = full_store.copy(tmp_path / "work")
    before = _state(work)
    with pytest.raises(SystemExit, match=named):
        _ingest(monkeypatch, work, other)
    assert _state(work) == before and "trial" not in before[1]


def test_a_record_with_an_error_entry_stops_a_run_before_the_bundle_opens(monkeypatch, tmp_path, pristine):
    store = _inputs(tmp_path, "inputs")
    store.record.write_text(json.dumps({"sample": "kidney", **pristine, "qc": {"error": "ValueError: x"}}))
    work = Store(tmp_path / "work")
    work.directory.mkdir()
    monkeypatch.setattr(ingest, "open_bundle", lambda location: pytest.fail("the bundle opened"))
    with pytest.raises(SystemExit, match=r"error entries at qc"):
        _ingest(monkeypatch, work, store)
    assert not work.repo.exists()


def test_a_fixture_run_reads_no_record_and_builds_its_root_attributes_when_the_root_step_runs(monkeypatch, tmp_path):
    store = _inputs(tmp_path, "inputs")
    work = Store(tmp_path / "work")
    work.directory.mkdir()
    monkeypatch.chdir(work.directory)
    monkeypatch.setattr(sys, "argv", ["ingest.py", "--sample", "kidney", "--repo", str(work.repo),
                                      "--catalog", str(work.catalog), "--namespace", "ns", "--bundle", str(store.bundle),
                                      "--record", str(tmp_path / "absent.json"), "--elements", "root"])
    ingest.main()
    root = zarr.open_group(open_repository(work.repo).readonly_session("fixture").store, mode="r")
    assert root.attrs["fixture"] == ingest.FIXTURE and root.attrs["title"] == SAMPLES["kidney"].title


# The preflight


def test_a_preflight_reports_a_record_that_load_record_refuses_as_a_problem(tmp_path):
    problems = _preflight(tmp_path, branch="new", sample="colorectal")
    assert len(problems) == 1 and "colorectal" in problems[0] and "qc.rasters" in problems[0]
    assert list(tmp_path.iterdir()) == []


def test_a_preflight_of_a_clean_record_has_no_problem_for_it(tmp_path):
    assert _preflight(tmp_path, branch="new", sample="brain") == []


def _preflight_cli(monkeypatch, tmp_path, *args):
    base = ["ingest.py", "--repo", str(tmp_path / "repo"), "--catalog", str(tmp_path / "catalog"), "--namespace", "ns",
            "--mode", "full", "--branch", "main", "--preflight", *args]
    monkeypatch.setattr(sys, "argv", base)
    monkeypatch.setattr(ingest, "open_bundle", lambda location: pytest.fail("the bundle opened"))
    with pytest.raises(SystemExit) as exit:
        ingest.main()
    return exit.value.code


def test_the_preflight_of_the_command_line_fails_for_a_sample_whose_record_is_refused(monkeypatch, capsys, tmp_path):
    assert _preflight_cli(monkeypatch, tmp_path, "--sample", "breast") == 1
    out = capsys.readouterr().out
    assert re.search(r"^PROBLEM: .*qc\.rasters", out, re.MULTILINE) and "preflight: 1 problem" in out


def test_the_preflight_of_the_command_line_takes_the_record_from_the_option(monkeypatch, capsys, tmp_path, pristine):
    write_record("breast", pristine, tmp_path)
    assert _preflight_cli(monkeypatch, tmp_path, "--sample", "breast", "--record", str(tmp_path / "breast.json")) == 0
    assert "PROBLEM" not in capsys.readouterr().out
