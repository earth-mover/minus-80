"""The ``change`` metadata every commit on top of ``source-copy`` carries.

PLAN.md decision 15 keeps tags for major checkpoints only, so each commit has
to describe itself. Its message says what changed, why and how it was checked,
for a person reading the history. Its metadata holds the same facts as fields,
for a program reading it. This module builds those fields the same way for
every improvement script.

A commit's ``code`` field names the git commit of the script that made it. That
is only true if the script on disk is the committed one, so :func:`code_ref`
refuses to run from a script with uncommitted edits.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = "arraylake://earthmover-biology/zebrafish-3d-4i"
BRANCH = "enhance"
PARENT_TAG = "source-copy"


def code_ref(*scripts: Path) -> dict:
    """The git commit and repository paths of ``scripts``, which must be committed."""
    top = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=scripts[0].parent,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    )
    paths = [str(s.resolve().relative_to(top)) for s in scripts]
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", *paths], cwd=top,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        raise SystemExit(f"commit these first, so the change can name its code:\n{dirty}")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=top, check=True, capture_output=True, text=True
    ).stdout.strip()
    return {"commit": sha, "paths": paths}


def change(
    *,
    plan_decision: int,
    summary: str,
    reason: str,
    kind: str,
    scope: dict,
    chunks_written: int,
    details: dict,
    checked: str,
    code: dict,
) -> dict:
    """The commit metadata for one improvement, with the fields of PLAN.md decision 15."""
    if kind not in ("metadata", "pixels"):
        raise ValueError(f"kind is 'metadata' or 'pixels', not {kind!r}")
    return {
        "change": {
            "plan_decision": plan_decision,
            "summary": summary,
            "reason": reason,
            "kind": kind,
            "scope": scope,
            "chunks_written": chunks_written,
            "details": details,
            "checked": checked,
            "parent_tag": PARENT_TAG,
            "code": code,
        }
    }


def enhance_session(repo):
    """A writable session on :data:`BRANCH`, which starts at :data:`PARENT_TAG` if it is new."""
    if BRANCH not in repo.list_branches():
        repo.create_branch(BRANCH, repo.lookup_tag(PARENT_TAG))
    return repo.writable_session(BRANCH)
