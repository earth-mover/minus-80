import json
import re
import shutil

import pytest

import survey
from bundle import Bundle
from facts import bundle_facts, load_record
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, SAMPLES
from survey import MAX_LINE, main, read_records, record_lines
from tests.test_facts import ALIGNMENT_TEXT, synthetic  # noqa: F401


def _lay_out(base, synthetic, names):  # noqa: F811
    """The three objects of each sample under ``base``, at the path that ``Sample.url`` gives."""
    path, he = synthetic
    for name in names:
        sample = SAMPLES[name]
        directory = base / sample.version / sample.name
        directory.mkdir(parents=True)
        shutil.copy(path, directory / f"{sample.name}{BUNDLE}")
        shutil.copy(he, directory / f"{sample.name}{HE_IMAGE}")
        (directory / f"{sample.name}{HE_ALIGNMENT}").write_text(ALIGNMENT_TEXT)


def _run(monkeypatch, tmp_path, synthetic, laid, asked):  # noqa: F811
    monkeypatch.setenv("MINUS80_S3_ANONYMOUS", "1")
    _lay_out(tmp_path / "s3", synthetic, laid)
    monkeypatch.setattr(survey, "S3", str(tmp_path / "s3"))
    return main(["--samples", ",".join(asked), "--out", str(tmp_path / "out")])


def test_survey_prints_one_record_per_sample_and_goes_on_after_a_failure(monkeypatch, capsys, tmp_path, synthetic):  # noqa: F811
    code = _run(monkeypatch, tmp_path, synthetic, ["kidney", "brain"], ["kidney", "colorectal", "brain"])
    out = capsys.readouterr().out
    assert code == 1
    with open(synthetic[1], "rb") as he:
        expected = bundle_facts(Bundle(synthetic[0]), he, ALIGNMENT_TEXT)
    assert read_records(out) == {"kidney": expected, "brain": expected}
    assert [x for x in out.splitlines() if x.startswith("===")] == [
        "=== record kidney begin", "=== record kidney end", "=== record brain begin", "=== record brain end"]
    assert re.search(r"^colorectal: FAILED \w+: .+$", out, re.MULTILINE)
    assert len(re.findall(r"^(kidney|brain): \d+ bytes read in \d+ requests$", out, re.MULTILINE)) == 2
    assert load_record("kidney", tmp_path / "out") == expected
    assert not (tmp_path / "out" / "colorectal.json").exists()


def test_survey_exits_zero_when_every_sample_passed(monkeypatch, capsys, tmp_path, synthetic):  # noqa: F811
    assert _run(monkeypatch, tmp_path, synthetic, ["breast"], ["breast"]) == 0
    assert "FAILED" not in capsys.readouterr().out


def test_a_record_line_is_one_line_between_the_markers_when_it_is_short():
    lines = record_lines("brain", {"b": [1], "a": "x"})
    assert lines == ["=== record brain begin", '{"a":"x","b":[1]}', "=== record brain end"]


def _long_record():
    return {"grid_keys": [f"{i},{j}" for i in range(60) for j in range(60)], "text": "a  b " * 3000}


def test_a_long_record_splits_into_numbered_parts_that_read_back_whole():
    record = _long_record()
    lines = record_lines("breast", record)
    parts = lines[1:-1]
    assert len(parts) > 2 and len(json.dumps(record)) > 2 * MAX_LINE
    for i, line in enumerate(parts, 1):
        assert re.match(rf"=== record breast part {i} of {len(parts)}: ", line)
        assert len(line.split(": ", 1)[1]) <= MAX_LINE and not line.endswith(" ")
    assert read_records("\n".join(lines)) == {"breast": record}


def test_read_records_takes_duplicated_reordered_and_unrelated_lines():
    long, short = record_lines("breast", _long_record()), record_lines("kidney", {"k": 1})
    log = ["tile 0,0: 5 rows", *long, "step done", *short, *reversed(long), *short, "=== record brain begin",
           "brain: FAILED ValueError: no", "=== record brain end"]
    assert read_records("\n".join(log)) == {"breast": _long_record(), "kidney": {"k": 1}}


def test_read_records_refuses_a_record_with_a_missing_part():
    lines = record_lines("breast", _long_record())
    with pytest.raises(ValueError, match="breast.*part 2"):
        read_records("\n".join(lines[:2] + lines[3:]))
