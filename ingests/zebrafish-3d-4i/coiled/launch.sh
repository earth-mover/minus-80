#!/usr/bin/env bash
# Convert wells of S-BIAD3228 on one Coiled VM, streaming the zip from EBI
# and committing to a branch of earthmover-biology/zebrafish-3d-4i.
#   ARRAYLAKE_TOKEN=... bash launch.sh <branch> <well,well,...>
# DIRECTORY_CACHE names the Parquet copy of the zip's central directory that
# ingest.py keeps (its --directory-cache). The job is given the copy, because
# reading the 179 MB directory from EBI can take many minutes.
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, and the job prints the commit it ran
# from, so each run in ../runs/ can name the code that wrote the store.
set -euo pipefail
(( $# == 2 )) || { echo "usage: launch.sh <branch> <well,well,...>" >&2; exit 2; }
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the repo}"
BRANCH=$1 WELLS=$2
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)
DIRECTORY_CACHE=${DIRECTORY_CACHE:-${MINUS80_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/minus80}/zebrafish-3d-4i/central-directory.parquet}
[[ -f $DIRECTORY_CACHE ]] || { echo "no central directory at $DIRECTORY_CACHE; set DIRECTORY_CACHE" >&2; exit 1; }

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/zebrafish-3d-4i pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/zebrafish-3d-4i/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
NAME="zebrafish-$(echo "$WELLS" | tr -c 'A-Za-z0-9\n' '-' | cut -c1-24)-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT/ingests/zebrafish-3d-4i/ingest.py" "$HERE/run.sh" "$STAGE/"
cp "$DIRECTORY_CACHE" "$STAGE/central-directory.parquet"
echo "$SHA" > "$STAGE/COMMIT"
echo "staged commit $SHA for $WELLS: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last. eu-west-2 (London) is
# the AWS region nearest EBI.
coiled batch run --name "$NAME" --region eu-west-2 \
    --vm-type m6i.4xlarge --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    run.sh "$BRANCH" "$WELLS"
echo "job $NAME: follow with coiled batch status and coiled batch logs"
