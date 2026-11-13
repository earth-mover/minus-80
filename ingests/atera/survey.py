"""Read the facts of each Atera bundle by range request and print one record for each sample.

    survey.py --samples kidney,brain,colorectal,breast --out <directory> [--max-bytes N]

A record prints as numbered ``part`` lines between a ``begin`` line and an ``end`` line.
The ``end`` line holds the SHA-256 and the character count of the record's JSON text.
``read_records`` joins the parts from a log and checks both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from check_bundle import open_sources
from facts import DEFAULT_MAX_BYTES, bundle_facts, error_text, write_record
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, S3, SAMPLES

#: The longest printed line, prefix included. A log service can cut a longer line.
MAX_LINE = 8_000
_BEGIN = re.compile(r"=== record (\S+) begin")
_PART = re.compile(r"=== record (\S+) part (\d+) of (\d+): (.*)")
_END = re.compile(r"=== record (\S+) end sha256=([0-9a-f]{64}) chars=(\d+)")
_FAILED = re.compile(r"(\S+): FAILED \w+: .*")


def _split(text: str, size: int) -> list[str]:
    """``text`` in parts of at most ``size`` characters. A part does not end in a space, which a log can strip.

    The function splits a text with no other split point at any place.
    """
    parts, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        cut = end
        while cut < len(text) and cut > start and text[cut - 1] == " ":
            cut -= 1
        cut = cut if cut > start else end
        parts.append(text[start:cut])
        start = cut
    return parts


def record_lines(sample: str, found: dict) -> list[str]:
    """The lines that print ``found``, markers included. Each line has at most ``MAX_LINE`` characters."""
    text = json.dumps(found, sort_keys=True, separators=(",", ":"))
    total = 1
    # The prefix of a part has its widest number at the last part.
    while len(parts := _split(text, MAX_LINE - len(f"=== record {sample} part {total} of {total}: "))) > total:
        total += 1
    digest = hashlib.sha256(text.encode()).hexdigest()
    return [
        f"=== record {sample} begin",
        *(f"=== record {sample} part {i} of {len(parts)}: {part}" for i, part in enumerate(parts, 1)),
        f"=== record {sample} end sha256={digest} chars={len(text)}",
    ]


def _joined(sample: str, begun: set, parts: dict, ends: dict) -> dict:
    def fail(what):
        return ValueError(f"record of {sample}: {what}")

    if sample not in begun | parts.keys() | ends.keys():
        raise fail("no marker line at the start of a line (a log with a prefix on each line is not read)")
    if sample not in begun:
        raise fail("no begin line")
    total, texts = parts.get(sample, (0, {}))
    if not texts:
        raise fail("no part lines")
    if missing := [i for i in range(1, total + 1) if i not in texts]:
        raise fail(f"part {missing[0]} of {total} is missing")
    if sample not in ends:
        raise fail("no end line")
    text = "".join(texts[i] for i in range(1, total + 1))
    digest, chars = ends[sample]
    if len(text) != chars:
        raise fail(f"the parts hold {len(text)} characters, the end line says {chars}")
    if hashlib.sha256(text.encode()).hexdigest() != digest:
        raise fail("the digest of the parts differs from the end line's")
    return json.loads(text)


def failed_samples(log_text: str) -> set[str]:
    """The samples with a ``FAILED`` line in ``log_text``."""
    return {m[1] for line in log_text.split("\n") if (m := _FAILED.fullmatch(line.rstrip()))}


def read_records(log_text: str, samples: Sequence[str] | None = None) -> dict[str, dict]:
    """The record of each sample in ``log_text``. The log can hold unrelated lines and identical duplicate lines.

    A sample needs a ``begin`` line, every part and an ``end`` line with a matching count and digest.
    A sample that fails any of these raises ``ValueError`` with its name. Duplicates that disagree raise it too.
    A ``FAILED`` line before the last ``begin`` line leaves a partial record, and the function returns it.
    A ``FAILED`` line after the last ``begin`` line, or with no ``begin`` line, raises.
    The result holds the samples in ``samples``, or every sample in the log when ``samples`` is ``None``.
    A sample in ``samples`` with no marker line at the start of a line raises.
    """
    begun, parts, ends, begin_at, failed_at = set(), {}, {}, {}, {}
    for index, line in enumerate(log_text.split("\n")):
        line = line.rstrip()
        if match := _BEGIN.fullmatch(line):
            begun.add(match[1])
            begin_at[match[1]] = index
        elif match := _PART.fullmatch(line):
            sample, number, total, text = match[1], int(match[2]), int(match[3]), match[4]
            seen_total, texts = parts.setdefault(sample, (total, {}))
            if not 1 <= number <= total or seen_total != total or texts.setdefault(number, text) != text:
                raise ValueError(f"record of {sample}: the lines of part {number} of {total} disagree")
        elif match := _END.fullmatch(line):
            if ends.setdefault(match[1], (match[2], int(match[3]))) != (match[2], int(match[3])):
                raise ValueError(f"record of {match[1]}: the end lines disagree")
        elif match := _FAILED.fullmatch(line):
            failed_at.setdefault(match[1], index)
    wanted = list(samples) if samples is not None else sorted(begun | parts.keys() | ends.keys())
    for sample in wanted:
        if sample in failed_at and begin_at.get(sample, -1) < failed_at[sample]:
            raise ValueError(f"record of {sample}: a FAILED line follows its last begin line")
    return {sample: _joined(sample, begun, parts, ends) for sample in wanted}


def survey_sample(name: str, directory: Path, max_bytes: int) -> bool:
    """Write and print the record of sample ``name``, then the bytes and requests that it took.

    When its reads pass ``max_bytes``, the groups not read are error entries. The record prints after a ``FAILED`` line.
    Returns false for such a sample.
    """
    sample = SAMPLES[name]
    bundle, open_he, alignment_text, counters, ceiling = open_sources(
        sample.url(BUNDLE, S3), sample.url(HE_IMAGE, S3), sample.url(HE_ALIGNMENT, S3), max_bytes)

    found = bundle_facts(bundle, open_he, alignment_text)
    errors = write_record(name, found, directory)
    if ceiling.message:
        print(f"{name}: FAILED ByteLimit: {ceiling.message}; a partial record follows", flush=True)
    for line in record_lines(name, found):
        print(line, flush=True)
    each = ", ".join(f"{label} {c.bytes} bytes in {c.requests} requests" for label, c in counters.items())
    print(f"{name}: {sum(c.bytes for c in counters.values())} bytes read in "
          f"{sum(c.requests for c in counters.values())} requests ({each})", flush=True)
    if errors:
        print(f"{name}: error entries at {', '.join(errors)}", flush=True)
    return ceiling.message is None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", required=True, help="the samples to survey, separated by commas")
    parser.add_argument("--out", required=True, type=Path, help="the directory for the record files")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="the ceiling of reads for one sample")
    args = parser.parse_args(argv)
    names = args.samples.split(",")
    if unknown := [n for n in names if n not in SAMPLES]:
        parser.error(f"unknown samples {', '.join(unknown)}; known: {', '.join(SAMPLES)}")
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    failed = 0
    for name in names:
        try:
            failed += not survey_sample(name, args.out, args.max_bytes)
        except Exception as error:
            failed += 1
            print(f"{name}: FAILED {error_text(error)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
