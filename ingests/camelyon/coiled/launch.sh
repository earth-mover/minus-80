#!/usr/bin/env bash
# Run a CAMELYON job on one Coiled VM in us-west-2, the bucket's region.
#   ARRAYLAKE_TOKEN=... [JOB=ingest.sh] bash launch.sh
# JOB names the job script in this directory: run.sh copies
# earthmover-public/camelyon into earthmover-biology/camelyon (the default),
# ingest.sh adds a challenge's slides to earthmover-biology/camelyon. A
# script under ../runs/ sets RUN to its own path, which the job prints beside
# the git commit, and any variables its job reads.
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, so each run in ../runs/ can name the
# code that wrote the target.
set -euo pipefail
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the target repo}"
VM_TYPE=${VM_TYPE:-c6in.2xlarge}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/camelyon pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/camelyon/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
JOB=${JOB:-run.sh}
NAME="camelyon-${JOB%.sh}-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT"/ingests/camelyon/*.py "$HERE"/*.sh "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "${RUN:-}" > "$STAGE/RUN"
echo "staged commit $SHA: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last. us-west-2 is the region
# of the CAMELYON bucket, which the verify step reads through the references.
coiled batch run --name "$NAME" --region us-west-2 \
    --vm-type "$VM_TYPE" --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    --env "CHALLENGE=${CHALLENGE:-}" \
    "$JOB"
echo "job $NAME"
