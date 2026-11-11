#!/usr/bin/env bash
# Scan, build and verify SanctSound deployments on one Coiled VM, committing
# each to main of earthmover-biology/sanctsound.
#   ARRAYLAKE_TOKEN=... bash launch.sh <deployment>...
# A script under ../runs/ sets RUN to its own path, which each Icechunk commit
# records beside the git commit.
# VM_TYPE overrides the VM. A whole-file scan needs about 6.5 CPU-seconds per
# GB, so the default is a c6in.4xlarge (docs/formats.md, "Budget CPU for the
# download as well as the parse").
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package or this ingest, and the job prints the commit it ran
# from, so each run in ../runs/ can name the code that wrote the store.
set -euo pipefail
(( $# )) || { echo "usage: launch.sh <deployment>..." >&2; exit 2; }
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to the repo}"
VM_TYPE=${VM_TYPE:-c6in.4xlarge}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/sanctsound pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/sanctsound/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
NAME="sanctsound-$(echo "$1" | tr 'A-Z_' 'a-z-')-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
cp "$ROOT"/ingests/sanctsound/{ingest.py,storage.py,verify.py} "$HERE/run.sh" "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "${RUN:-}" > "$STAGE/RUN"
echo "staged commit $SHA for $*: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last. us-east-1 is the AWS
# region nearest the bucket's Google Cloud US multi-region.
coiled batch run --name "$NAME" --region us-east-1 \
    --vm-type "$VM_TYPE" --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    run.sh "$@"
echo "job $NAME"
