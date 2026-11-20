#!/usr/bin/env bash
# Run an IDC job on one Coiled VM in us-east-1, the region of s3://idc-open-data.
#   ARRAYLAKE_TOKEN=... COLLECTION=gtex TARGET=arraylake://org/repo bash launch.sh
# A script under ../runs/ sets RUN to its own path, which the job prints beside
# the git commit, and the variables its job reads.
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, so each run in ../runs/ can name the
# code that wrote the target.
set -euo pipefail
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the target repo}"
: "${COLLECTION:?set COLLECTION, e.g. gtex}"
: "${TARGET:?set TARGET to arraylake://<org>/<repo>}"
VM_TYPE=${VM_TYPE:-c6in.4xlarge}
JOB=${JOB:-ingest.sh}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/idc pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/idc/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
NAME="idc-${COLLECTION}-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
# The job installs exactly the versions uv.lock resolves for this ingest,
# git sources included, and the wheel without its own dependencies.
(cd "$ROOT" && uv export --package idc-ingest --no-hashes --no-emit-workspace \
    --no-dev --frozen -q -o "$STAGE/requirements.txt")
cp "$ROOT"/ingests/idc/*.py "$HERE"/*.sh "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "${RUN:-}" > "$STAGE/RUN"
echo "staged commit $SHA: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last.
coiled batch run --name "$NAME" --region us-east-1 \
    --vm-type "$VM_TYPE" --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    --env "COLLECTION=$COLLECTION" --env "TARGET=$TARGET" \
    "$JOB"
echo "job $NAME"
