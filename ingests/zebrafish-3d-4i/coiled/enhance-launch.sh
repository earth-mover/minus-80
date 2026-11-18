#!/usr/bin/env bash
# Run one of the enhance/ scripts on a Coiled VM, beside the store.
#   ARRAYLAKE_TOKEN=... bash enhance-launch.sh <script.py> [args...]
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, and passes the commit to the job as
# MINUS80_CODE_COMMIT, which the script records in its commit's metadata.
set -euo pipefail
(( $# >= 1 )) || { echo "usage: enhance-launch.sh <script.py> [args...]" >&2; exit 2; }
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the repo}"
SCRIPT=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)
[[ -f $ROOT/ingests/zebrafish-3d-4i/enhance/$SCRIPT ]] || { echo "no enhance/$SCRIPT" >&2; exit 2; }

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/zebrafish-3d-4i pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/zebrafish-3d-4i/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
NAME="zebrafish-${SCRIPT%.py}-$(date -u +%H%M%S)"
NAME=${NAME//_/-}

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT"/ingests/zebrafish-3d-4i/enhance/*.py "$HERE/enhance-run.sh" "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "staged commit $SHA for $SCRIPT $*: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch rather than a detached coiled run, and one --upload of the
# whole staging directory: see launch.sh. m6i.8xlarge gives 32 cores and
# 12.5 Gbit/s for a job that decodes and encodes on every core while it reads.
coiled batch run --name "$NAME" --region eu-west-2 \
    --vm-type m6i.8xlarge --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    enhance-run.sh "$SCRIPT" "$@"
echo "job $NAME: follow with coiled batch status and coiled batch logs"
