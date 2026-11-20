"""Spot-check a MICrONS two-photon store against BossDB's listing and DANDI's NWB files.

``test_ingest.py`` checks the ingest itself on a small scan where every frame
is cheap to compare. This script checks what depends on the real archive, in
about a minute, through both exports' commits on main, which it finds by
their commit metadata:

- ``complete``: every scan in BossDB's listing has a group, with the fields
  its NWB file states, and each scan's scene places every one of them. One LIST
  request.
- ``schema``, ``dimension names``, ``physical size``: the shared OME-Zarr gates.
- ``times``: each sampled scan's ``volume_times`` equals the NWB timestamps,
  and its scene places each field at the center and depth the NWB states.
- ``pixels``: in two scans, two fields each, a 14 x 14 patch of two frames
  equals the NWB file's copy of the same frame, transposed. The NWB copy is
  decoded by h5py from DANDI's gzip chunks, so the check runs through a
  second publication of the movie, not back through the TIFF the store
  references. Each patch is one NWB chunk.

    uv run --package microns-ingest python ingests/microns/verify.py \\
        --repo <dir | arraylake://org/repo> [--tag]

With ``--tag``, and only when every check passes, the two commits are tagged
``v1`` and ``v2``. Icechunk does not let a deleted tag's name be used again,
so the tags wait for the check rather than being moved off a bad commit.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

import h5py
import ingest
import numpy as np
import zarr
from plumbing import list_prefix

from minus80.core import remote
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, open_repository
from minus80.ome.verify import GATES, run_gates

#: The exports, by the names their commits are tagged with.
TAGS = tuple(e["export"] for e in ingest.EXPORTS.values())

#: The NWB series' chunks are 14 x 14 pixels in space, so a patch this size
#: aligned to them costs one chunk read.
PATCH = 14


def complete(root, names: list[str]) -> list[str]:
    """Every scan BossDB lists has its group and fields, and its scene places every field."""
    problems = []
    scans = root[ingest.SCANS]
    for name in names:
        if name not in scans:
            problems.append(f"no group for scan {name}")
            continue
        attrs = dict(scans[name].attrs)
        placed = {t["input"]["path"] for t in attrs["ome"]["scene"]["coordinateTransformations"]}
        for n in range(1, attrs["scan"]["fields"] + 1):
            path = f"{ingest.SCANS}/{name}/field-{n}"
            if f"field-{n}" not in scans[name]:
                problems.append(f"{path}: missing")
            if f"field-{n}" not in placed:
                problems.append(f"{path}: not placed by the scan's scene")
    return problems


def times(root, name: str, nwb) -> list[str]:
    """``volume_times`` equals the NWB timestamps, and the scene places each field as the NWB states."""
    problems = []
    group = root[f"{ingest.SCANS}/{name}"]
    if not np.array_equal(group["volume_times"][:], nwb["acquisition/TwoPhotonSeries1/timestamps"][:]):
        problems.append(f"{name}: volume_times differ from the NWB timestamps")
    transforms = {t["input"]["path"]: t["translation"]
                  for t in dict(group.attrs)["ome"]["scene"]["coordinateTransformations"]}
    for path, (t, z, y, x) in transforms.items():
        n = int(path.removeprefix("field-"))
        origin = nwb[f"general/optophysiology/ImagingPlane{n}/origin_coords"][()] * 1e6
        level = group[f"{path}/0"]
        pixel = nwb[f"general/optophysiology/ImagingPlane{n}/grid_spacing"][()][0] * 1e6
        center_y = y + (level.shape[2] - 1) / 2 * pixel
        center_x = x + (level.shape[3] - 1) / 2 * pixel
        if not np.allclose([center_x, center_y, z], origin, atol=1e-6):
            problems.append(f"{name}/{path}: placed at center ({center_x}, {center_y}, {z}), "
                            f"the NWB states {origin.tolist()}")
    return problems


def pixels(roots: dict[str, zarr.Group], name: str, nwb, *, rng: random.Random) -> list[str]:
    """Two frames of two fields equal the NWB copy, through every tag.

    The first export declares the same bytes uint16, so its patch is compared
    with the NWB values as uint16.
    """
    problems = []
    first = roots[TAGS[0]][f"{ingest.SCANS}/{name}"]
    count = dict(first.attrs)["scan"]["fields"]
    for n in sorted(rng.sample(range(1, count + 1), min(2, count))):
        source = nwb[f"acquisition/TwoPhotonSeries{n}/data"]
        volumes, columns, rows = source.shape
        size = min(PATCH, rows, columns)
        for v in sorted(rng.sample(range(volumes), 2)):
            r = rng.randrange(rows // size) * size
            c = rng.randrange(columns // size) * size
            want = source[v, c:c + size, r:r + size].T
            for tag, root in roots.items():
                got = root[f"{ingest.SCANS}/{name}/field-{n}/0"][v, 0, r:r + size, c:c + size]
                if not np.array_equal(got, want.astype(got.dtype)):
                    problems.append(f"{tag} {name}/field-{n} volume {v} rows {r}:{r + size} "
                                    f"columns {c}:{c + size}: differs from the NWB copy")
            print(f"          {name} field-{n} volume {v}: patch at ({r}, {c})", flush=True)
    return problems


def open_exports(location: str, branch: str | None = None):
    """The repo, and each export's commit on main as a read-only group, keyed by its tag name.

    With ``branch``, the last export is read at the branch's head instead of
    its own commit, so that later metadata-only commits on top of it are
    checked too; the head's root must name that export.
    """
    repo = arraylake_repository(location) if location.startswith(ARRAYLAKE) else open_repository(location)
    snapshots = ingest.committed_exports(repo)
    if missing := sorted(set(ingest.EXPORTS) - set(snapshots)):
        raise SystemExit(f"main holds no commit for exports {missing}")
    roots = {ingest.EXPORTS[e]["export"]: zarr.open_group(
        repo.readonly_session(snapshot_id=snapshot.id).store, mode="r")
        for e, snapshot in sorted(snapshots.items())}
    if branch is not None:
        head = zarr.open_group(repo.readonly_session(branch).store, mode="r")
        last = TAGS[-1]
        if dict(head.attrs)["tiff_export"]["export"] != last:
            raise SystemExit(f"{branch}'s head does not hold export {last}")
        roots[last] = head
    return repo, roots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--scans", help="the scans the store should hold, comma-separated; default "
                                        "every scan BossDB lists")
    parser.add_argument("--sample", type=int, default=2, help="scans sampled for times and pixels")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--branch", help="read the last export at this branch's head, not its own commit")
    parser.add_argument("--tag", action="store_true",
                        help="tag the two commits v1 and v2 if every check passes")
    args = parser.parse_args()
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    started = time.monotonic()
    if args.tag and args.branch:
        raise SystemExit("--tag tags the exports' own commits; run it without --branch")
    repo, roots = open_exports(args.repo, args.branch)
    names = sorted({f"{m['session']}-{m['scan']}" for n in list_prefix(ingest.SOURCE)
                    if (m := ingest.TIFF_NAME.match(n))})
    if args.scans:
        names = sorted(set(args.scans.split(",")) & set(names))
    rng = random.Random(args.seed)
    sampled = sorted(rng.sample(names, min(args.sample, len(names))))

    def sampled_check(check):
        def gate(root, context):
            problems = []
            for name in sampled:
                nwb_url = dict(root[f"{ingest.SCANS}/{name}"].attrs)["scan"]["nwb"]
                with h5py.File(remote.reader(nwb_url), "r") as nwb:
                    problems += check(root, name, nwb)
            return problems
        return gate

    gates = {
        "complete": lambda root, n: complete(root, names),
        "schema": GATES["schema"],
        "dimension names": GATES["dimension names"],
        "physical size": GATES["physical size"],
        "times": sampled_check(times),
        "pixels": sampled_check(lambda root, name, nwb: pixels(roots, name, nwb, rng=rng)),
    }
    failed = 0
    for tag in TAGS:
        print(f"export {tag}", flush=True)
        failed += run_gates(roots[tag], gates if tag == TAGS[-1] else
                            {k: v for k, v in gates.items() if k != "pixels"},
                            {"version": ingest.VERSION})
    print(f"{'FAILED' if failed else 'passed'} in {time.monotonic() - started:.0f} s")
    if args.tag and not failed:
        for tag, snapshot in ingest.tag_exports(repo).items():
            print(f"tag {tag}: {snapshot}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
