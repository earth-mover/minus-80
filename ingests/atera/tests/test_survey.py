import hashlib
import io
import json
import re
import shutil

import pytest

import survey
from bundle import Bundle
from facts import bundle_facts, load_record
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, SAMPLES
from survey import MAX_LINE, _split, failed_samples, main, read_records, record_lines
from tests.test_facts import ALIGNMENT_TEXT, synthetic  # noqa: F401


def _lay_out(base, synthetic, names):  # noqa: F811
    """The three objects of each sample under ``base``, at the path that ``Sample.url`` gives."""
    for name in names:
        sample = SAMPLES[name]
        directory = base / sample.version / sample.name
        directory.mkdir(parents=True)
        shutil.copy(synthetic.path, directory / f"{sample.name}{BUNDLE}")
        shutil.copy(synthetic.he, directory / f"{sample.name}{HE_IMAGE}")
        (directory / f"{sample.name}{HE_ALIGNMENT}").write_text(ALIGNMENT_TEXT)


def _run(monkeypatch, tmp_path, synthetic, laid, asked, *options):  # noqa: F811
    monkeypatch.setenv("MINUS80_S3_ANONYMOUS", "1")
    _lay_out(tmp_path / "s3", synthetic, laid)
    monkeypatch.setattr(survey, "S3", str(tmp_path / "s3"))
    return main(["--samples", ",".join(asked), "--out", str(tmp_path / "out"), *options])


def _expected(synthetic):  # noqa: F811
    he_bytes = synthetic.he.read_bytes()
    return bundle_facts(Bundle(synthetic.path), lambda: io.BytesIO(he_bytes), lambda: ALIGNMENT_TEXT)


def test_survey_prints_one_record_per_sample_and_goes_on_after_a_failure(monkeypatch, capsys, tmp_path, synthetic):  # noqa: F811
    code = _run(monkeypatch, tmp_path, synthetic, ["kidney", "brain"], ["kidney", "colorectal", "brain"])
    out = capsys.readouterr().out
    assert code == 1
    expected = _expected(synthetic)
    assert read_records(out, ["kidney", "brain"]) == {"kidney": expected, "brain": expected}
    assert [x.split(" sha256=")[0] for x in out.splitlines() if x.endswith(" begin") or " end sha256=" in x] == [
        "=== record kidney begin", "=== record kidney end", "=== record brain begin", "=== record brain end"]
    assert all(len(x) <= MAX_LINE for x in out.splitlines())
    failed = re.search(r"^colorectal: FAILED \w+: .+$", out, re.MULTILINE)
    assert failed and str(tmp_path) not in failed[0]
    summary = re.search(r"^kidney: (\d+) bytes read in (\d+) requests \(bundle (\d+) bytes in (\d+) requests, "
                        r"he_image (\d+) bytes in (\d+) requests, alignment (\d+) bytes in (\d+) requests\)$",
                        out, re.MULTILINE)
    assert summary and int(summary[1]) == int(summary[3]) + int(summary[5]) + int(summary[7])
    assert int(summary[5]) > 0 and int(summary[7]) > 0
    assert load_record("kidney", tmp_path / "out") == expected
    assert not (tmp_path / "out" / "colorectal.json").exists()


def test_survey_exits_zero_when_every_sample_passed(monkeypatch, capsys, tmp_path, synthetic):  # noqa: F811
    assert _run(monkeypatch, tmp_path, synthetic, ["breast"], ["breast"]) == 0
    assert "FAILED" not in capsys.readouterr().out


def test_a_missing_he_object_leaves_the_bundle_groups_in_the_record(monkeypatch, capsys, tmp_path, synthetic):  # noqa: F811
    _lay_out(tmp_path / "s3", synthetic, ["brain"])
    sample = SAMPLES["brain"]
    (tmp_path / "s3" / sample.version / sample.name / f"{sample.name}{HE_IMAGE}").unlink()
    monkeypatch.setenv("MINUS80_S3_ANONYMOUS", "1")
    monkeypatch.setattr(survey, "S3", str(tmp_path / "s3"))
    assert main(["--samples", "brain", "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    record = read_records(out, ["brain"])["brain"]
    assert list(record["he_image"]) == ["error"] and str(tmp_path) not in record["he_image"]["error"]
    assert record["transcripts"]["number_rnas"] and "error entries at he_image" in out


def _limit_run(monkeypatch, tmp_path, synthetic, *options):  # noqa: F811
    _lay_out(tmp_path / "s3", synthetic, ["kidney", "brain"])
    monkeypatch.setenv("MINUS80_S3_ANONYMOUS", "1")
    monkeypatch.setattr(survey, "S3", str(tmp_path / "s3"))
    return main(["--samples", "kidney,brain", "--out", str(tmp_path / "out"), *options])


def test_a_sample_over_its_byte_ceiling_fails_and_leaves_a_partial_record_and_the_run_goes_on(
        monkeypatch, capsys, tmp_path, synthetic):  # noqa: F811
    code = _limit_run(monkeypatch, tmp_path, synthetic, "--max-bytes", "20000")
    out = capsys.readouterr().out
    assert code == 1
    assert len(re.findall(r"^(kidney|brain): FAILED ByteLimit: read \d+ bytes, over the limit of 20000 bytes; "
                          r"a partial record follows$", out, re.MULTILINE)) == 2
    records = read_records(out, ["kidney", "brain"])
    assert failed_samples(out) == {"kidney", "brain"}
    for name in ("kidney", "brain"):
        assert records[name]["members"] and records[name]["transcripts"]["error"].startswith("ByteLimit: read ")
        assert (tmp_path / "out" / f"{name}.json").exists()


def test_a_ceiling_passed_late_keeps_every_earlier_group(monkeypatch, capsys, tmp_path, synthetic):  # noqa: F811
    assert _limit_run(monkeypatch, tmp_path, synthetic) == 0
    total = int(re.search(r"^kidney: (\d+) bytes read", capsys.readouterr().out, re.MULTILINE)[1])
    for part in (tmp_path / "s3", tmp_path / "out"):
        shutil.rmtree(part)
    assert _limit_run(monkeypatch, tmp_path, synthetic, "--max-bytes", str(total - 1)) == 1
    out = capsys.readouterr().out
    record = read_records(out, ["kidney"])["kidney"]
    assert record["transcripts"]["number_rnas"] and record["stores"]["cell_feature_matrix.zarr.zip"]["error"]
    assert "kidney: error entries at" in out and "kidney" in failed_samples(out)


# The log transport.

def _long_record():
    return {"grid_keys": [f"{i},{j}" for i in range(60) for j in range(60)], "text": "a  b " * 3000}


def test_a_short_record_is_one_numbered_part_between_the_markers():
    lines = record_lines("brain", {"b": [1], "a": "x"})
    text = '{"a":"x","b":[1]}'
    assert lines == ["=== record brain begin", f"=== record brain part 1 of 1: {text}",
                     f"=== record brain end sha256={hashlib.sha256(text.encode()).hexdigest()} chars={len(text)}"]
    assert read_records("\n".join(lines)) == {"brain": {"a": "x", "b": [1]}}


def test_a_long_record_splits_into_parts_that_fit_the_line_bound_and_read_back_whole():
    record = _long_record()
    lines = record_lines("breast", record)
    parts = lines[1:-1]
    assert len(parts) > 5 and all(len(line) <= MAX_LINE for line in lines) and MAX_LINE == 8_000
    for i, line in enumerate(parts, 1):
        assert line.startswith(f"=== record breast part {i} of {len(parts)}: ") and not line.endswith(" ")
    assert read_records("\n".join(lines)) == {"breast": record}


def test_split_goes_on_when_the_text_has_no_place_to_split():
    parts = _split(" " * 23, 5)
    assert "".join(parts) == " " * 23 and max(map(len, parts)) <= 5
    assert _split("ab  cd", 3) == ["ab", "  c", "d"]


def _log(*samples):
    return [line for sample, record in samples for line in record_lines(sample, record)]


def test_read_records_takes_duplicated_reordered_and_unrelated_lines():
    long, short = record_lines("breast", _long_record()), record_lines("kidney", {"k": 1})
    log = ["tile 0,0: 5 rows", *long, "step done", *short, long[0], *reversed(long[1:-1]), long[-1], *short, long[3],
           "=== record brain begin", "brain: FAILED ValueError: no"]
    assert read_records("\n".join(log), ["kidney", "breast"]) == {"kidney": {"k": 1}, "breast": _long_record()}


def test_read_records_strips_a_trailing_space_and_a_carriage_return_of_each_line():
    log = "\r\n".join(line + " " for line in record_lines("kidney", {"k": 1}))
    assert read_records(log) == {"kidney": {"k": 1}}


def test_a_truncated_part_fails_the_character_count_and_a_changed_part_fails_the_digest():
    lines = record_lines("breast", _long_record())
    with pytest.raises(ValueError, match="breast.*characters"):
        read_records("\n".join([lines[0], lines[1][:-5], *lines[2:]]))
    with pytest.raises(ValueError, match="breast.*digest"):
        read_records("\n".join([lines[0], lines[1][:-1] + ("a" if lines[1][-1] != "a" else "b"), *lines[2:]]))


def test_a_missing_part_a_missing_end_and_a_missing_begin_raise():
    lines = record_lines("breast", _long_record())
    with pytest.raises(ValueError, match="breast.*part 2 of"):
        read_records("\n".join(lines[:2] + lines[3:]))
    with pytest.raises(ValueError, match="breast.*no end line"):
        read_records("\n".join(lines[:-1]))
    with pytest.raises(ValueError, match="breast.*no begin line"):
        read_records("\n".join(lines[1:]))


def test_a_sample_that_was_asked_for_and_has_no_lines_raises_and_others_are_not_needed():
    log = "\n".join(record_lines("kidney", {"k": 1}))
    with pytest.raises(ValueError, match="brain.*no marker line at the start of a line"):
        read_records(log, ["kidney", "brain"])
    assert read_records(log, ["kidney"]) == {"kidney": {"k": 1}}


def test_duplicated_parts_that_disagree_raise():
    lines = record_lines("breast", _long_record())
    other = lines[1][:-1] + ("a" if lines[1][-1] != "a" else "b")
    with pytest.raises(ValueError, match="breast.*disagree"):
        read_records("\n".join([*lines, other]))


def test_a_record_of_exactly_one_part_at_the_line_bound():
    room = MAX_LINE - len("=== record s part 1 of 1: ")
    record = {"t": "x" * (room - len('{"t":""}'))}
    lines = record_lines("s", record)
    assert len(lines) == 3 and len(lines[1]) == MAX_LINE and read_records("\n".join(lines)) == {"s": record}
    assert json.dumps(record, separators=(",", ":")) == lines[1].split(": ", 1)[1]


def test_a_log_with_a_prefix_on_each_line_is_not_read_and_the_error_says_so():
    log = "\n".join("[12:00:01] " + line for line in record_lines("kidney", {"k": 1}))
    with pytest.raises(ValueError, match="kidney.*no marker line at the start of a line"):
        read_records(log, ["kidney"])


def test_a_failed_line_after_an_older_complete_record_raises_and_one_before_a_partial_record_does_not():
    older = record_lines("kidney", {"k": 1})
    log = "\n".join([*older, "kidney: FAILED OSError: gone"])
    with pytest.raises(ValueError, match="kidney.*FAILED"):
        read_records(log, ["kidney"])
    partial = "\n".join(["kidney: FAILED ByteLimit: read 9 bytes, over the limit of 1 bytes; a partial record follows",
                         *older, "kidney: FAILED ByteLimit: read 9 bytes, over the limit of 1 bytes; a partial record follows"])
    assert read_records(partial, ["kidney"]) == {"kidney": {"k": 1}}


def test_failed_samples_lists_each_sample_with_a_failed_line():
    log = "\n".join(["tile 0,0: 5 rows", "kidney: FAILED OSError: gone", "brain: 5 bytes read in 2 requests",
                     "kidney: FAILED OSError: gone", "breast: FAILED ByteLimit: read 2 bytes, over the limit of 1 bytes"])
    assert failed_samples(log) == {"kidney", "breast"}
