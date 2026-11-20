#!/usr/bin/env bash
# Run the IBL ingest on one Coiled VM in us-east-1.
#   ARRAYLAKE_TOKEN=... RUN=<runs script> TAG=<git tag> bash launch.sh
# A script under ../runs/ sets RUN to its own path and TAG to the git tag that
# names the run's commit; the job prints both beside the commit. To resume a
# stopped run, add a new runs/ script with a new TAG: the job skips every pass
# already tagged in the repo and every session already written.
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package, this ingest or the lockfile, and refuses a commit
# that is not on the remote. It tags the commit and pushes the tag before the
# job starts, so a later rebase of the branch cannot orphan the code that
# wrote the store.
set -euo pipefail
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to earthmover-biology/ibl-brain-wide-map}"
: "${RUN:?set RUN to the runs/ script that launches this}"
: "${TAG:?set TAG to the git tag that names the commit of this run}"
VM_TYPE=${VM_TYPE:-c6in.xlarge}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/ibl-brain-wide-map pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/ibl-brain-wide-map/ or the lockfile; commit them first" >&2
    exit 1
fi
SHA=$(git -C "$ROOT" rev-parse HEAD)
if [[ -z $(git -C "$ROOT" branch -r --contains "$SHA") ]]; then
    echo "commit $SHA is not on the remote; push it first" >&2
    exit 1
fi
if git -C "$ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    echo "tag $TAG already exists; a new run needs a new tag" >&2
    exit 1
fi
git -C "$ROOT" tag -a "$TAG" -m "IBL Brain-Wide Map: the code $RUN ran" "$SHA"
git -C "$ROOT" push -q origin "refs/tags/$TAG"
NAME="ibl-bwm-$(date -u +%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
# The job installs exactly the versions uv.lock resolves for this ingest,
# git sources included, and the wheel without its own dependencies.
(cd "$ROOT" && uv export --package ibl-brain-wide-map-ingest --no-hashes --no-emit-workspace \
    --no-dev --frozen -q -o "$STAGE/requirements.txt")
cp "$ROOT"/ingests/ibl-brain-wide-map/{ingest.py,sources.py,verify.py} "$HERE/run.sh" "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "$RUN ($TAG)" > "$STAGE/RUN"
echo "staged commit $SHA, tagged $TAG: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last. us-east-1 holds the IBL
# bucket, whose files the job reads whole (the spike columns' last chunks);
# from DANDI's bucket in us-east-2 the job reads only file headers and a
# verify window.
coiled batch run --name "$NAME" --region us-east-1 \
    --vm-type "$VM_TYPE" --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    run.sh
echo "job $NAME: follow with coiled batch status and coiled batch logs"
