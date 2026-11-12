"""Move branch main of a repo forward to the snapshot of a verified trial branch.

The move is a reset of main, guarded to fail if main moved since the read.
The snapshot of main must be an ancestor of the trial snapshot, so no commit of main is lost.
The script never deletes a branch, a tag or a snapshot.
"""

from __future__ import annotations

import argparse
import sys

from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, open_repository

TRIAL_PREFIX = "trial-"


def promote(repo, source: str, target: str = "main", *, dry_run: bool = False) -> tuple[str, str]:
    """Return (old snapshot of target, snapshot of source); move target unless equal or dry_run."""
    if not source.startswith(TRIAL_PREFIX):
        raise ValueError(f"the source {source} does not start with {TRIAL_PREFIX}")
    if target != "main":
        raise ValueError("the target must be main")
    branches = repo.list_branches()
    for name in (source, target):
        if name not in branches:
            raise ValueError(f"the branch {name} does not exist")
    old, new = repo.lookup_branch(target), repo.lookup_branch(source)
    if old == new:
        return old, new
    if old not in (info.id for info in repo.ancestry(snapshot_id=new)):
        raise ValueError(f"the snapshot {old} of {target} is not an ancestor of the snapshot {new} of {source}")
    if dry_run:
        return old, new
    # from_snapshot_id makes the reset fail if target moved since the read above.
    repo.reset_branch(target, new, from_snapshot_id=old)
    if (now := repo.lookup_branch(target)) != new:
        raise RuntimeError(f"{target} is at {now} after the reset, not at {new}")
    return old, new


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="a local directory, or arraylake://<org>/<repo>")
    parser.add_argument("--from", dest="source", required=True, help="the trial branch, trial-<name>")
    parser.add_argument("--to", dest="target", default="main", help="the branch to move; only main")
    parser.add_argument("--dry-run", action="store_true", help="print the move and change nothing")
    args = parser.parse_args()
    try:
        repo = arraylake_repository(args.repo) if args.repo.startswith(ARRAYLAKE) else open_repository(args.repo)
        old, new = promote(repo, args.source, args.target, dry_run=args.dry_run)
    except ValueError as refusal:
        _fail(str(refusal))
    except Exception as error:
        # A service message can hold a storage prefix, so only the type shows.
        _fail(type(error).__name__)
    if old == new:
        print(f"{args.target} is at {new} already")
    elif args.dry_run:
        print(f"{args.target}: would move {old} -> {new}")
    else:
        print(f"{args.target}: {old} -> {new}")
    sys.exit(0)


if __name__ == "__main__":
    main()
