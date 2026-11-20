#!/usr/bin/env bash
# Ingest the MICrONS two-photon scans on one Coiled VM in us-east-1, beside
# the BossDB bucket, committing both exports to the repo named,
# arraylake://earthmover-biology/microns for every run so far planned.
#   ARRAYLAKE_TOKEN=... bash launch.sh arraylake://earthmover-biology/microns [scan,scan,...]
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, and the job prints the commit it ran
# from, so each run in ../runs/ can name the code that wrote the store.
set -euo pipefail
(( $# >= 1 && $# <= 2 )) || { echo "usage: launch.sh <arraylake://org/repo> [scan,scan,...]" >&2; exit 2; }
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the repo}"
REPO=$1 SCANS=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/microns pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/microns/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
NAME="microns-twophoton-$(date -u +%m%d-%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT"/ingests/microns/{ingest.py,plumbing.py,verify.py} "$HERE/run.sh" "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "staged commit $SHA: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last. us-east-1 is the BossDB
# bucket's region; the NWB files are in us-east-2, and only their metadata is
# read.
coiled batch run --name "$NAME" --region us-east-1 \
    --vm-type c6i.4xlarge --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    run.sh "$REPO" "$SCANS"
echo "job $NAME: follow with coiled batch status and coiled batch logs"
