"""Restate a MICrONS store's group metadata as ingest.py now writes it, in one commit.

    uv run --package microns-ingest python ingests/microns/scenes.py \\
        --repo <dir | arraylake://org/repo> [--branch main]

The store's first layout made its root a ``bioformats2raw.layout`` collection,
with an ``OME`` group listing every field image beside the hierarchy that
already holds them. The current layout declares the fields only through each
scan's OME-Zarr 0.6 scene (LAYOUTS.md). This script brings an existing store
to the current layout without rewriting any array:

- the ``OME`` group is deleted;
- the root's, ``two-photon``'s, each scan group's and each field image's
  attributes are set to what ``ingest.py`` writes, from the same functions,
  with the NWB metadata read again and each field's ``tiff`` attributes kept
  as stored.

Only group attributes change and one group goes, so no array's shape, dtype,
codecs or chunk references move. A group whose attributes already match is
left alone, and nothing is committed when nothing differs, so a second run
is a no-op. The commit carries no export in its metadata: ingest.py and
verify.py find each export by its own commit, below this one.
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

from minus80.core.icechunk_io import commit

MESSAGE = ("Describe each scan as an OME-Zarr 0.6 scene and drop the transitional "
           "bioformats2raw listing. The root no longer carries a bioformats2raw.layout "
           "collection and the OME group that listed all {fields} field images is gone; each "
           "scan group's scene names its own field images by path, and the root and "
           "two-photon groups describe what is below them. Only group metadata changes: no "
           "array's shape, dtype, codecs or chunk references.")


def scan_from_store(attrs: dict) -> ingest.Scan:
    """The scan a scan group's ``scan`` attribute describes."""
    exports = {e["export"]: n for n, e in ingest.EXPORTS.items()}
    return ingest.Scan(attrs["animal"], attrs["session"], attrs["scan"],
                       tiffs={exports[name]: url for name, url in attrs["tiff"].items()},
                       nwb=attrs["nwb"], nwb_path=attrs["nwb_dandi_path"])


def as_stored(attrs: dict) -> dict:
    """``attrs`` as they read back from a zarr.json, so tuples compare equal to lists."""
    return json.loads(json.dumps(attrs))


def restate(repo, branch: str = "main") -> str | None:
    """Commit the current layout's group metadata on ``branch``; the snapshot id, or None if nothing changed."""
    session = repo.writable_session(branch)
    root = zarr.open_group(session.store, mode="a")
    exports = {e["export"]: n for n, e in ingest.EXPORTS.items()}
    export = exports[dict(root.attrs)["tiff_export"]["export"]]
    scans_group = root[ingest.SCANS]
    stored = [scan_from_store(dict(scans_group[name].attrs)["scan"])
              for name in scans_group.group_keys()]
    stored.sort(key=lambda s: (s.animal, s.session, s.scan))
    with ThreadPoolExecutor(8) as pool:
        facts = list(pool.map(ingest.read_nwb, stored))

    changed = []

    def put(group, attrs: dict, path: str) -> None:
        if dict(group.attrs) != as_stored(attrs):
            group.attrs.put(attrs)
            changed.append(path)

    put(root, ingest.root_attributes(export), "/")
    if "OME" in root:
        del root["OME"]
        changed.append("OME (deleted)")
    put(scans_group, ingest.scans_attributes(facts), ingest.SCANS)
    for f in facts:
        base = f"{ingest.SCANS}/{f.scan.name}"
        put(root[base], ingest.scan_attributes(f), base)
        for field in f.fields:
            image = root[f"{base}/field-{field.number}"]
            attrs = ingest.field_attributes(image["0"].shape, field, f, export,
                                            dict(image.attrs)["tiff"])
            put(image, attrs, f"{base}/field-{field.number}")
    if not changed:
        print("the store already has the current layout; nothing to commit", flush=True)
        return None
    print(f"{len(changed)} changes: {', '.join(changed[:6])}{' ...' if len(changed) > 6 else ''}",
          flush=True)
    fields = sum(len(f.fields) for f in facts)
    snapshot = commit(session, MESSAGE.format(fields=fields),
                      metadata={"restated": "group metadata in the scene layout"})
    print(f"committed {snapshot}", flush=True)
    return snapshot


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    restate(open_for_writing(args.repo, ingest.SOURCE), args.branch)


if __name__ == "__main__":
    sys.exit(main())
