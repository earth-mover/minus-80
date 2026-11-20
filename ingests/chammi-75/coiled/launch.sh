#!/usr/bin/env bash
# Ingest the allowlisted CHAMMI-75 studies on one Coiled VM in us-west-2,
# beside the bucket, committing to a branch of earthmover-biology/chammi-75.
#   ARRAYLAKE_TOKEN=... bash launch.sh <branch> [study,study,...]
# Without studies, every allowlisted study is ingested.
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, and the job prints the commit it ran
# from, so each run in ../runs/ can name the code that wrote the store.
set -euo pipefail
(( $# == 1 || $# == 2 )) || { echo "usage: launch.sh <branch> [study,study,...]" >&2; exit 2; }
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the repo}"
BRANCH=$1 STUDIES=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/chammi-75 pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/chammi-75/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
NAME="chammi-$(echo "${STUDIES:-all}" | tr -c 'A-Za-z0-9\n' '-' | cut -c1-24)-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT"/ingests/chammi-75/{ingest.py,pipeline.py,verify.py} "$HERE/run.sh" "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "staged commit $SHA for ${STUDIES:-every allowlisted study}: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last. us-west-2 is the
# bucket's region, so every read of the source stays in-region.
coiled batch run --name "$NAME" --region us-west-2 \
    --vm-type m6i.4xlarge --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    run.sh "$BRANCH" "$STUDIES"
echo "job $NAME: follow with coiled batch status and coiled batch logs"
