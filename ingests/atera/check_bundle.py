"""Compare one Atera bundle with the committed record of its sample and print each difference.

    check_bundle.py --sample S [--bundle PATH] [--he-image PATH] [--he-alignment PATH] [--record PATH]
                    [--skip-groups a,b] [--max-bytes N]

By default the bundle, the H&E image and the alignment are the sample's objects, read by range request.
A group in ``--skip-groups`` is neither read nor compared. The exit code is 0 for no difference,
1 for a difference, and 2 for a record that cannot serve as a reference.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Collection
from pathlib import Path

from bundle import Bundle, he_readers, open_bundle
from facts import DEFAULT_MAX_BYTES, Ceiling, bundle_facts, compare, load_record
from minus80.core.remote import ReadCounter
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, S3, SAMPLES


def differences(record: dict, found: dict, skip: Collection[str] = ()) -> list[str]:
    """The lines where ``found`` differs from ``record``, with the groups in ``skip`` left out of both."""
    return compare({k: v for k, v in record.items() if k not in skip}, {k: v for k, v in found.items() if k not in skip})


def check(
    bundle: Bundle, open_he: Callable, alignment_text: Callable, sample_name: str, *,
    record: dict | None = None, skip: Collection[str] = (),
) -> list[str]:
    """The difference lines between ``bundle`` and ``record``, which is the sample's committed record by default."""
    record = load_record(sample_name) if record is None else record
    return differences(record, bundle_facts(bundle, open_he, alignment_text, skip), skip)


def open_sources(
    bundle_location: str, he_location: str, alignment_location: str, max_bytes: int = DEFAULT_MAX_BYTES,
):
    """The bundle, ``open_he``, ``alignment_text``, a read counter for each of the three, and the ceiling of their reads."""
    bundle, bundle_counter = open_bundle(bundle_location)
    ceiling = Ceiling(max_bytes)
    ceiling.total = bundle_counter.bytes
    counters = {"bundle": bundle_counter, "he_image": ReadCounter(), "alignment": ReadCounter()}
    for counter in counters.values():
        counter.progress = ceiling
    open_he, alignment_text = he_readers(
        he_location, alignment_location, counters["he_image"].wrap, counters["alignment"].wrap)
    return bundle, open_he, alignment_text, counters, ceiling


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", choices=sorted(SAMPLES), required=True)
    parser.add_argument("--bundle", help="a local outs.zip or a URL; by default the sample's object")
    parser.add_argument("--he-image", help="the H&E OME-TIFF, a URL or a local path; by default the sample's object")
    parser.add_argument("--he-alignment", help="the alignment CSV, a URL or a local path; by default the sample's object")
    parser.add_argument("--record", type=Path, help="the record file; by default the sample's committed record")
    parser.add_argument("--skip-groups", default="", help="groups to leave out of the check, separated by commas")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="the ceiling of the bytes read")
    args = parser.parse_args(argv)
    skip = [g for g in args.skip_groups.split(",") if g]
    try:
        record = load_record(args.sample, path=args.record)
        if unknown := [g for g in skip if g not in record]:
            raise ValueError(f"no group {', '.join(unknown)} on record; the groups are {', '.join(record)}")
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    sample = SAMPLES[args.sample]
    bundle, open_he, alignment_text, counters, _ = open_sources(
        args.bundle or sample.url(BUNDLE, S3), args.he_image or sample.url(HE_IMAGE, S3),
        args.he_alignment or sample.url(HE_ALIGNMENT, S3), args.max_bytes)
    lines = check(bundle, open_he, alignment_text, args.sample, record=record, skip=skip)
    for line in lines:
        print(line)
    print(f"bundle check: {len(lines)} differences")
    each = ", ".join(f"{label} {c.bytes} bytes in {c.requests} requests" for label, c in counters.items())
    print(f"read {sum(c.bytes for c in counters.values())} bytes in {sum(c.requests for c in counters.values())} requests ({each})")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
