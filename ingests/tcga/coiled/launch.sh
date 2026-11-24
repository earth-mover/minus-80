#!/usr/bin/env bash
# Ingest TCGA slides on one Coiled VM in us-east-1, beside IDC's bucket, into
# earthmover-biology/tcga.
#   ARRAYLAKE_TOKEN=... bash launch.sh <collection> <limit> [branch]
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, and the job prints the commit it ran
# from, so each run in ../runs/ can name the code that wrote the store.
set -euo pipefail
(( $# >= 2 )) || { echo "usage: launch.sh <collection> <limit> [branch]" >&2; exit 2; }
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the repo}"
COLLECTION=$1 LIMIT=$2 BRANCH=${3:-main}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/tcga pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/tcga/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
NAME="tcga-$COLLECTION-$LIMIT-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT"/ingests/tcga/{ingest.py,slides.py} "$HERE/run.sh" "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "staged commit $SHA: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last.
coiled batch run --name "$NAME" --region us-east-1 \
    --vm-type m6i.4xlarge --disk-size 200 --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    run.sh "$COLLECTION" "$LIMIT" "$BRANCH"
echo "job $NAME: follow with coiled batch status and coiled batch logs"
