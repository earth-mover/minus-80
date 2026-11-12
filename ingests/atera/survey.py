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

from facts import bundle_facts, error_text, write_record
from ingest import open_bundle
from minus80.core.registry import as_url
from minus80.core.remote import ReadCounter, reader
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, S3, SAMPLES

#: The longest printed line, prefix included. A log service can cut a longer line.
MAX_LINE = 8_000
DEFAULT_MAX_BYTES = 200_000_000
_BEGIN = re.compile(r"=== record (\S+) begin")
_PART = re.compile(r"=== record (\S+) part (\d+) of (\d+): (.*)")
_END = re.compile(r"=== record (\S+) end sha256=([0-9a-f]{64}) chars=(\d+)")


class ByteLimit(BaseException):
    """A sample read more than its ceiling. It is no ``Exception``, so no fact group records it."""


class _Ceiling:
    """The progress hook of a read counter. It stops a sample that reads more than ``limit`` bytes."""

    def __init__(self, limit: int):
        self.limit, self.total = limit, 0

    def advance(self, size: int) -> None:
        self.total += size
        if self.total > self.limit:
            raise ByteLimit(f"read {self.total} bytes, over the limit of {self.limit} bytes")


def _split(text: str, size: int) -> list[str]:
    """``text`` in parts of at most ``size`` characters. A part does not end in a space, which a log could strip.

    A text with no other place to split is split anywhere.
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
        raise fail("no lines")
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


def read_records(log_text: str, samples: Sequence[str] | None = None) -> dict[str, dict]:
    """The record of each sample in ``log_text``. The log can hold unrelated lines and identical duplicate lines.

    A sample needs a ``begin`` line, every part and an ``end`` line with a matching count and digest.
    A sample that fails any of these raises ``ValueError`` that names it. Duplicates that disagree raise it too.
    The result holds the samples in ``samples``, or every sample in the log when ``samples`` is ``None``.
    A sample in ``samples`` with no line raises.
    """
    begun, parts, ends = set(), {}, {}
    for line in log_text.split("\n"):
        line = line.rstrip()
        if match := _BEGIN.fullmatch(line):
            begun.add(match[1])
        elif match := _PART.fullmatch(line):
            sample, number, total, text = match[1], int(match[2]), int(match[3]), match[4]
            seen_total, texts = parts.setdefault(sample, (total, {}))
            if not 1 <= number <= total or seen_total != total or texts.setdefault(number, text) != text:
                raise ValueError(f"record of {sample}: the lines of part {number} of {total} disagree")
        elif match := _END.fullmatch(line):
            if ends.setdefault(match[1], (match[2], int(match[3]))) != (match[2], int(match[3])):
                raise ValueError(f"record of {match[1]}: the end lines disagree")
    wanted = list(samples) if samples is not None else sorted(begun | parts.keys() | ends.keys())
    return {sample: _joined(sample, begun, parts, ends) for sample in wanted}


def survey_sample(name: str, directory: Path, max_bytes: int) -> None:
    """Write and print the record of sample ``name``, then the bytes and requests that it took.

    The sample stops with ``ByteLimit`` when its reads pass ``max_bytes``.
    """
    sample = SAMPLES[name]
    ceiling = _Ceiling(max_bytes)
    bundle, bundle_counter = open_bundle(sample.url(BUNDLE, S3))
    ceiling.total = bundle_counter.bytes
    he_counter, alignment_counter = ReadCounter(), ReadCounter()
    counters = {"bundle": bundle_counter, "he_image": he_counter, "alignment": alignment_counter}
    for counter in counters.values():
        counter.progress = ceiling

    def open_he():
        return reader(as_url(sample.url(HE_IMAGE, S3)), wrap=he_counter.wrap)

    def alignment_text():
        return reader(as_url(sample.url(HE_ALIGNMENT, S3)), wrap=alignment_counter.wrap).read().decode()

    found = bundle_facts(bundle, open_he, alignment_text)
    errors = write_record(name, found, directory)
    for line in record_lines(name, found):
        print(line, flush=True)
    each = ", ".join(f"{label} {c.bytes} bytes in {c.requests} requests" for label, c in counters.items())
    print(f"{name}: {sum(c.bytes for c in counters.values())} bytes read in "
          f"{sum(c.requests for c in counters.values())} requests ({each})", flush=True)
    if errors:
        print(f"{name}: error entries at {', '.join(errors)}", flush=True)


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
            survey_sample(name, args.out, args.max_bytes)
        except ByteLimit as limit:
            failed += 1
            print(f"{name}: FAILED ByteLimit: {limit}", flush=True)
        except Exception as error:
            failed += 1
            print(f"{name}: FAILED {error_text(error)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
