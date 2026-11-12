"""Read the facts of each Atera bundle by range request and print one record for each sample.

    survey.py --samples kidney,brain,colorectal,breast --out <directory>

A record is one JSON line between ``=== record <sample> begin`` and ``=== record <sample> end``.
A line over ``MAX_LINE`` characters becomes numbered ``part`` lines; ``read_records`` joins them from a log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from facts import bundle_facts, write_record
from ingest import open_bundle
from minus80.core.registry import as_url
from minus80.core.remote import reader
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, S3, SAMPLES

#: The longest text of one printed line; a log service can cut a longer line.
MAX_LINE = 16_000
_BEGIN, _END = re.compile(r"=== record (\S+) begin"), re.compile(r"=== record (\S+) end")
_PART = re.compile(r"=== record (\S+) part (\d+) of (\d+): (.*)", re.DOTALL)


def _split(text: str) -> list[str]:
    """``text`` in pieces of at most ``MAX_LINE`` characters, none ending in a space that a log could strip."""
    parts, start = [], 0
    while start < len(text):
        end = min(start + MAX_LINE, len(text))
        while end < len(text) and text[end - 1] == " ":
            end -= 1
        parts.append(text[start:end])
        start = end
    return parts


def record_lines(sample: str, found: dict) -> list[str]:
    """The lines that print ``found``, markers included."""
    text = json.dumps(found, sort_keys=True, separators=(",", ":"))
    parts = _split(text)
    body = parts if len(parts) == 1 else [f"=== record {sample} part {i} of {len(parts)}: {p}" for i, p in enumerate(parts, 1)]
    return [f"=== record {sample} begin", *body, f"=== record {sample} end"]


def read_records(log_text: str) -> dict[str, dict]:
    """The record of each sample in ``log_text``, which can hold duplicate lines and unrelated lines.

    A one-line record follows its ``begin`` line. A part line names its sample, so parts can come in any order.
    A record with a missing part raises ``ValueError``.
    """
    records, parts, inside = {}, {}, None
    for line in log_text.splitlines():
        if match := _BEGIN.fullmatch(line):
            inside = match[1]
        elif _END.fullmatch(line):
            inside = None
        elif match := _PART.fullmatch(line):
            sample, number, total, text = match[1], int(match[2]), int(match[3]), match[4]
            if parts.setdefault(sample, (total, {}))[0] != total or parts[sample][1].setdefault(number, text) != text:
                raise ValueError(f"record of {sample}: the lines of part {number} of {total} disagree")
        elif inside and line.startswith("{"):
            records[inside] = json.loads(line)
    for sample, (total, texts) in parts.items():
        if missing := [i for i in range(1, total + 1) if i not in texts]:
            raise ValueError(f"record of {sample}: part {missing[0]} of {total} is missing")
        records[sample] = json.loads("".join(texts[i] for i in range(1, total + 1)))
    return records


def survey_sample(name: str, directory: Path) -> None:
    """Write and print the record of sample ``name``, then the bytes and requests that it took."""
    sample = SAMPLES[name]
    bundle, counter = open_bundle(sample.url(BUNDLE, S3))
    he_file = reader(as_url(sample.url(HE_IMAGE, S3)), wrap=counter.wrap)
    alignment = reader(as_url(sample.url(HE_ALIGNMENT, S3)), wrap=counter.wrap).read().decode()
    found = bundle_facts(bundle, he_file, alignment)
    write_record(name, found, directory)
    for line in record_lines(name, found):
        print(line, flush=True)
    print(f"{name}: {counter.bytes} bytes read in {counter.requests} requests", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", required=True, help="the samples to survey, separated by commas")
    parser.add_argument("--out", required=True, type=Path, help="the directory for the record files")
    args = parser.parse_args(argv)
    names = args.samples.split(",")
    if unknown := [n for n in names if n not in SAMPLES]:
        parser.error(f"unknown samples {', '.join(unknown)}; known: {', '.join(SAMPLES)}")
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    failed = 0
    for name in names:
        try:
            survey_sample(name, args.out)
        except Exception as error:
            failed += 1
            print(f"{name}: FAILED {type(error).__name__}: {str(error).replace(chr(10), ' ')}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
