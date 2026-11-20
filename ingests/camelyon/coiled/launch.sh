#!/usr/bin/env bash
# Copy earthmover-public/camelyon into earthmover-biology/camelyon on one Coiled VM.
#   ARRAYLAKE_TOKEN=... bash launch.sh
# A script under ../runs/ sets RUN to its own path, which the job prints beside
# the git commit.
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
NAME="camelyon-transfer-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT"/ingests/camelyon/{transfer.py,verify_transfer.py} "$HERE/run.sh" "$STAGE/"
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
    run.sh
echo "job $NAME"
