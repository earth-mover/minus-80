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

import os
import subprocess
from pathlib import Path

REPO = "arraylake://earthmover-biology/zebrafish-3d-4i"
BRANCH = "enhance"
PARENT_TAG = "source-copy"
GITHUB = "https://github.com/earth-mover/minus-80"
PLAN = "ingests/zebrafish-3d-4i/PLAN.md"


#: Where the enhance scripts sit in the repository, for a job that runs them
#: from a copy outside any git checkout.
ENHANCE_DIR = "ingests/zebrafish-3d-4i/enhance"


def code_ref(*scripts: Path) -> dict:
    """The git commit and repository paths of ``scripts``, which must be committed.

    A cloud job has no checkout. Its launcher refuses uncommitted code and
    passes the commit as ``MINUS80_CODE_COMMIT``, which is used instead.
    """
    if sha := os.environ.get("MINUS80_CODE_COMMIT"):
        paths = [f"{ENHANCE_DIR}/{s.name}" for s in scripts]
        return {"commit": sha, "paths": paths, "urls": [f"{GITHUB}/blob/{sha}/{p}" for p in paths]}
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
    return {"commit": sha, "paths": paths, "urls": [f"{GITHUB}/blob/{sha}/{p}" for p in paths]}


def change(
    *,
    summary: str,
    reason: str,
    kind: str,
    scope: dict,
    chunks_written: int,
    details: dict,
    checked: str,
    code: dict,
) -> dict:
    """The commit metadata for one improvement, with the fields of PLAN.md decision 15.

    Commit messages and metadata are read by people who have never seen the
    plan, so they name no decision numbers; ``plan`` links the plan as it
    stood when the change was made.
    """
    if kind not in ("metadata", "pixels"):
        raise ValueError(f"kind is 'metadata' or 'pixels', not {kind!r}")
    return {
        "change": {
            "summary": summary,
            "reason": reason,
            "kind": kind,
            "scope": scope,
            "chunks_written": chunks_written,
            "details": details,
            "checked": checked,
            "parent_tag": PARENT_TAG,
            "code": code,
            "plan": f"{GITHUB}/blob/{code['commit']}/{PLAN}" if code else None,
        }
    }


def image_paths(root) -> list[str]:
    """Every image the plate lists, as ``well/field``, in plate order."""
    return [
        f"{well['path']}/{image['path']}"
        for well in root.attrs["ome"]["plate"]["wells"]
        for image in root[well["path"]].attrs["ome"]["well"]["images"]
    ]


def check_session(session, *, updated_groups=frozenset(), new_groups=frozenset(), new_arrays=frozenset(),
                  replaced_arrays=frozenset(), chunks_under=None) -> None:
    """Stop unless the session changed exactly the nodes named, before anything is committed.

    Paths are given without a leading slash. ``replaced_arrays`` were deleted
    and created again at the same path, which Icechunk may report as deleted
    and new or as updated. ``chunks_under`` is the set of arrays allowed to
    have written chunks; None means no chunk was written.
    """
    diff = session.status()
    replaced = set(replaced_arrays)
    if replaced:
        touched = {p.lstrip("/") for kind in ("deleted_arrays", "updated_arrays", "new_arrays")
                   for p in getattr(diff, kind)}
        if missing := replaced - touched:
            raise SystemExit(f"arrays not replaced: {sorted(missing)[:10]}")
        if extra := touched - replaced - set(new_arrays):
            raise SystemExit(f"arrays changed outside the plan: {sorted(extra)[:10]}")
        new_arrays = set(new_arrays) | ({p.lstrip("/") for p in diff.new_arrays} & replaced)
    for kind, expected in (("updated_groups", updated_groups), ("new_groups", new_groups),
                           ("new_arrays", new_arrays)):
        got = {p.lstrip("/") for p in getattr(diff, kind)}
        if got != set(expected):
            raise SystemExit(f"{kind} differ from the plan: {sorted(got ^ set(expected))[:10]}")
    chunked = {p.lstrip("/") for p in diff.updated_chunks}
    if chunked - set(chunks_under or ()):
        raise SystemExit(f"chunks written outside the plan: {sorted(chunked - set(chunks_under or ()))[:10]}")
    others = (diff.deleted_groups, diff.moved_nodes,
              *(() if replaced else (diff.deleted_arrays, diff.updated_arrays)))
    if any(others):
        raise SystemExit(f"the session deleted, moved or redefined nodes: {diff}")


def open_root(session, mode: str = "r", name: str = "icechunk"):
    """The session's root group and the meter counting its writes.

    zarr reaches the session through :class:`~minus80.core.metered.MeteredStore`,
    whose ``str()`` does not read the session; with many threads on one
    session, Icechunk's own ``str()`` can hang the process.
    """
    import zarr

    from minus80.core.metered import Meter, MeteredStore

    meter = Meter()
    return zarr.open_group(MeteredStore(session.store, meter, name=name), mode=mode), meter


def enhance_session(repo):
    """A writable session on :data:`BRANCH`, which starts at :data:`PARENT_TAG` if it is new."""
    if BRANCH not in repo.list_branches():
        repo.create_branch(BRANCH, repo.lookup_tag(PARENT_TAG))
    return repo.writable_session(BRANCH)
