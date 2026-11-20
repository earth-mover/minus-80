#!/usr/bin/env bash
# Run 2: restate earthmover-biology/MICrONS's group metadata in the scene
# layout, as one metadata-only commit on main, then verify.py at main's head.
#
# Run 1 wrote the root as a bioformats2raw.layout collection, with an OME
# group listing all 144 field images beside the hierarchy that holds them.
# scenes.py deletes that group and replaces the root's and two-photon's
# attributes with what ingest.py now writes; each scan group was already an
# OME-Zarr 0.6 scene and is left as it is. No array changes, and tags v1 and
# v2 stay on run 1's commits. A second run commits nothing.
#
# The dry run against the store (scenes.py --dry-run, 2026-11-20) listed
# exactly that: the root gains "layout" and loses "ome" (version 0.6,
# bioformats2raw.layout 3), two-photon's description changes, OME is
# deleted, and every scan group and field image already matches.
#
# It reads only metadata (the store's groups and 19 NWB files' imaging
# planes, about a minute), so it runs from any machine with the token. The
# script refuses uncommitted changes and a commit that is not pushed, and
# tags the commit it runs from microns-run2-scenes.
#
# Not yet run. When it runs, record here the date, the commit it ran from,
# the new snapshot and the verify result.
#   ARRAYLAKE_TOKEN=... bash runs/02_scenes.sh
set -euo pipefail
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the repo}"
REPO=arraylake://earthmover-biology/MICrONS
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
TAG=microns-run2-scenes
if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/microns pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/microns/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
git -C "$ROOT" fetch -q origin
if [[ -z $(git -C "$ROOT" branch -r --contains "$SHA") ]]; then
    echo "commit $SHA is not on any remote branch; push it first" >&2
    exit 1
fi
if TAGGED=$(git -C "$ROOT" rev-parse -q --verify "refs/tags/$TAG^{commit}"); then
    [[ $TAGGED == "$SHA" ]] || { echo "tag $TAG already names $TAGGED, not $SHA" >&2; exit 1; }
else
    git -C "$ROOT" tag -m "Code that ran MICrONS run 2 (scene layout restated in place in earthmover-biology/MICrONS)" "$TAG" "$SHA"
fi
git -C "$ROOT" push -q origin "refs/tags/$TAG"
echo "commit $SHA (tag $TAG), repo $REPO, start $(date -u +%FT%TZ)"
cd "$ROOT"
export MINUS80_S3_ANONYMOUS=1
uv run --package microns-ingest python ingests/microns/scenes.py --repo "$REPO"
timeout 30m uv run --package microns-ingest python ingests/microns/verify.py --repo "$REPO" --branch main
echo "end $(date -u +%FT%TZ)"
