"""Restate a MICrONS store's top-level metadata as ingest.py now writes it, in one commit.

    uv run --package microns-ingest python ingests/microns/scenes.py \\
        --repo <dir | arraylake://org/repo> [--branch main] [--dry-run]

The store's first layout made its root a ``bioformats2raw.layout`` collection,
with an ``OME`` group listing every field image beside the hierarchy that
already holds them. The current layout declares the fields only through each
scan's OME-Zarr 0.6 scene (LAYOUTS.md), which the first layout already wrote.
This script brings an existing store to the current layout without
rewriting any array, and changes only three nodes:

- the ``OME`` group is deleted, with ``del root["OME"]``;
- the root's and ``two-photon``'s attributes are replaced, with
  ``group.attrs.put``, by what ``ingest.py`` writes, from the same functions.

It also compares every scan group's and field image's attributes with what
``ingest.py`` would write now, from the NWB metadata read again and each
field's stored ``tiff`` attributes, and reports any difference key by key.
It never changes those groups: a difference there means the ingest's output
has changed since the store was written, which is a decision for a person,
not for a metadata fix.

``--dry-run`` reads the branch, prints every difference and the groups it
would delete, and commits nothing. Without it, nothing is committed when the
three nodes already match, so a second run is a no-op. The commit carries no
export in its metadata: ingest.py and verify.py find each export by its own
commit, below this one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import ingest
import zarr
from plumbing import open_for_writing

from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, open_repository

MESSAGE = ("Describe each scan as an OME-Zarr 0.6 scene and drop the transitional "
           "bioformats2raw listing. The root no longer carries a bioformats2raw.layout "
           "collection and the OME group that listed all {fields} field images is gone; each "
           "scan group's scene names its own field images by path, and the root and "
           "two-photon groups describe what is below them. Only the root's and two-photon's "
           "attributes change: no array's shape, dtype, codecs or chunk references.")

#: The groups the script may change, besides deleting ``OME``.
RESTATED = ("/", ingest.SCANS)


def scan_from_store(attrs: dict) -> ingest.Scan:
    """The scan a scan group's ``scan`` attribute describes."""
    exports = {e["export"]: n for n, e in ingest.EXPORTS.items()}
    return ingest.Scan(attrs["animal"], attrs["session"], attrs["scan"],
                       tiffs={exports[name]: url for name, url in attrs["tiff"].items()},
                       nwb=attrs["nwb"], nwb_path=attrs["nwb_dandi_path"])


def as_stored(attrs: dict) -> dict:
    """``attrs`` as they read back from a zarr.json, so tuples compare equal to lists."""
    return json.loads(json.dumps(attrs))


def attribute_diff(old: dict, new: dict) -> dict:
    """The top-level keys ``new`` adds, removes and changes relative to ``old``, with their values."""
    return {
        "added": {k: new[k] for k in sorted(set(new) - set(old))},
        "removed": {k: old[k] for k in sorted(set(old) - set(new))},
        "changed": {k: {"old": old[k], "new": new[k]}
                    for k in sorted(set(old) & set(new)) if old[k] != new[k]},
    }


def plan(root) -> tuple[dict[str, tuple[dict, dict]], list[str], dict[str, dict]]:
    """What the current layout would change under ``root``.

    Returns the restated groups whose attributes differ, as path to (old,
    new); the groups to delete; and the attribute diff of every scan group
    and field image that differs from what the ingest writes now, which is
    reported and left alone.
    """
    exports = {e["export"]: n for n, e in ingest.EXPORTS.items()}
    export = exports[dict(root.attrs)["tiff_export"]["export"]]
    scans_group = root[ingest.SCANS]
    stored = [scan_from_store(dict(scans_group[name].attrs)["scan"])
              for name in scans_group.group_keys()]
    stored.sort(key=lambda s: (s.animal, s.session, s.scan))
    with ThreadPoolExecutor(8) as pool:
        facts = list(pool.map(ingest.read_nwb, stored))

    restated = {}
    for path, group, attrs in (("/", root, ingest.root_attributes(export)),
                               (ingest.SCANS, scans_group, ingest.scans_attributes(facts))):
        old, new = dict(group.attrs), as_stored(attrs)
        if old != new:
            restated[path] = (old, new)
    deleted = ["OME"] if "OME" in root else []
    left = {}
    for f in facts:
        base = f"{ingest.SCANS}/{f.scan.name}"
        nodes = [(base, root[base], ingest.scan_attributes(f))]
        for field in f.fields:
            image = root[f"{base}/field-{field.number}"]
            nodes.append((f"{base}/field-{field.number}", image, ingest.field_attributes(
                image["0"].shape, field, f, export, dict(image.attrs)["tiff"])))
        for path, group, attrs in nodes:
            old, new = dict(group.attrs), as_stored(attrs)
            if old != new:
                left[path] = attribute_diff(old, new)
    return restated, deleted, left


def report(restated: dict, deleted: list[str], left: dict) -> None:
    for path, (old, new) in restated.items():
        print(f"restate {path}: {json.dumps(attribute_diff(old, new), indent=1)}", flush=True)
    for path in deleted:
        print(f"delete {path}", flush=True)
    if left:
        print(f"{len(left)} scan or field groups differ from what the ingest writes now; "
              f"left unchanged:", flush=True)
        for path, diff in left.items():
            print(f"  {path}: {json.dumps(diff)}", flush=True)
    else:
        print("every scan group and field image matches what the ingest writes now", flush=True)


def restate(repo, branch: str = "main", *, dry_run: bool = False) -> str | None:
    """Commit the restated top-level metadata on ``branch``; the snapshot id, or None if nothing changed."""
    session = repo.readonly_session(branch) if dry_run else repo.writable_session(branch)
    root = zarr.open_group(session.store, mode="r" if dry_run else "a")
    restated, deleted, left = plan(root)
    report(restated, deleted, left)
    if dry_run or not (restated or deleted):
        if not dry_run:
            print("the top-level groups already have the current layout; nothing to commit", flush=True)
        return None
    for path, (_, new) in restated.items():
        (root if path == "/" else root[path]).attrs.put(new)
    for path in deleted:
        del root[path]
    fields = sum(len(dict(g.attrs)["ome"]["scene"]["coordinateTransformations"])
                 for _, g in root[ingest.SCANS].groups())
    snapshot = commit(session, MESSAGE.format(fields=fields),
                      metadata={"restated": "root and two-photon metadata in the scene layout"})
    print(f"committed {snapshot}", flush=True)
    return snapshot


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true", help="print what would change and commit nothing")
    args = ap.parse_args()
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    if args.dry_run:
        repo = (arraylake_repository(args.repo) if args.repo.startswith(ARRAYLAKE)
                else open_repository(args.repo))
    else:
        repo = open_for_writing(args.repo, ingest.SOURCE)
    restate(repo, args.branch, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
