"""Reject files that contain local paths, cloud account identifiers or secrets.

Generic patterns live here. Extra patterns are read from the file named by
``LEAK_PATTERNS`` (default ``~/.config/minus-80/leak-patterns.txt``)
when it exists; its contents are never part of the repository. Each line is a
regex, optionally followed by a tab and a glob of paths where it is allowed.

Usage: ``check_leaks.py [--all] [FILE ...]``. ``--all`` scans every tracked
file and the full history.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

GENERIC = [
    r"/Users/[A-Za-z0-9._-]+/",
    r"/home/[A-Za-z0-9._-]+/",
    r"arn:aws:iam::\d{12}",
    r"(?i)(account|aws|role)\D{0,40}\b\d{12}\b",
    r"(?i)aws_secret_access_key\s*[=:]",
]

DEFAULT_PRIVATE = Path("~/.config/minus-80/leak-patterns.txt")

Rule = Tuple["re.Pattern[str]", Optional[str]]


def load_rules() -> list[Rule]:
    rules: list[Rule] = [(re.compile(p), None) for p in GENERIC]
    private = Path(os.environ.get("LEAK_PATTERNS", DEFAULT_PRIVATE)).expanduser()
    if private.exists():
        for line in private.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            regex, _, allow = line.partition("\t")
            rules.append((re.compile(regex.strip()), allow.strip() or None))
    return rules


def scan_text(name: str, text: str, rules: list[Rule]) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for number, (pattern, allow) in enumerate(rules):
            if allow and fnmatch.fnmatch(name, allow):
                continue
            if pattern.search(line):
                # Print the location and the rule number, never the match:
                # the output may land in CI logs.
                hits.append(f"{name}:{lineno}: matches rule {number}")
    return hits


def scan_history(rules: list[Rule]) -> list[str]:
    log = subprocess.run(
        ["git", "log", "--all", "-p", "--format=commit %H%n%an%n%B"],
        capture_output=True, text=True, check=True,
    ).stdout
    return scan_text("<history>", log, rules)


def main(argv: list[str]) -> int:
    rules = load_rules()
    if "--all" in argv:
        files = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True, check=True
        ).stdout.split()
        hits = scan_history(rules)
    else:
        files = argv
        hits = []
    for name in files:
        try:
            text = Path(name).read_text()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        hits += scan_text(name, text, rules)
    for hit in hits:
        print(hit)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
