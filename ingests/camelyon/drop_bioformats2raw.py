"""Remove the bioformats2raw layout from a store's CAMELYON17 container, in one commit.

Run 2 wrote CAMELYON17 as OME-Zarr 0.6 with a ``bioformats2raw.layout``
container: the layout key in the group's ``ome`` attribute and an ``OME``
group listing every image in ``series``. OME-Zarr 0.6 keeps that layout only
as transitional metadata, and the ingest no longer writes it for 0.6
(LAYOUTS.md). This removes both, through ``ingest.drop_bioformats2raw``, the
same function the ingest uses, and commits only if something changed, so a
second run does nothing. ``CAMELYON16`` is published as 0.5 and is not
touched.

    uv run --package camelyon-ingest python ingests/camelyon/drop_bioformats2raw.py \\
        --repo <dir | arraylake://org/repo>
"""

from __future__ import annotations

import argparse

import zarr
from ingest import C17, drop_bioformats2raw
from minus80.core.icechunk_io import commit
from plumbing import SOURCE, Source, open_repository, say

MESSAGE = ("CAMELYON17: remove the transitional bioformats2raw layout (the OME group and the "
           "bioformats2raw.layout key). OME-Zarr 0.6 does not require it; CAMELYON17's images are the "
           "multiscale groups under it, and its slide table lists them by path. CAMELYON16, OME-Zarr 0.5, "
           "keeps its layout.")


def run(location: str, source: Source = Source(SOURCE)) -> str | None:
    """The snapshot committed, or None when the container had no layout to remove."""
    repo = open_repository(location, source)
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    if C17.name not in root or not drop_bioformats2raw(root[C17.name]):
        say(f"{location}: {C17.name} has no bioformats2raw layout; nothing to commit")
        return None
    snapshot = commit(session, MESSAGE)
    say(f"{location}: committed {snapshot}")
    return snapshot


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="a local directory or arraylake://org/repo")
    run(ap.parse_args(argv).repo)


if __name__ == "__main__":
    main()
