#!/usr/bin/env bash
# Run phase 2 of the MICrONS ingest on one Coiled VM in us-east-2, beside
# DANDI's bucket, into arraylake://earthmover-biology/MICrONS.
#   ARRAYLAKE_TOKEN=... RUN=<runs script> TAG=<git tag> bash launch_processed.sh [scan,scan,...]
# A script under ../runs/ sets RUN to its own path and TAG to the git tag that
# names the run's commit. A relaunch resumes: processed.py skips every
# phase-2 commit main already holds.
#
# The job runs the committed code only: launching refuses a working tree with
# changes to the package, this ingest or the lockfile, and a commit that is
# not on the remote, and it tags and pushes the commit before the job starts.
set -euo pipefail
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to earthmover-biology/MICrONS}"
: "${RUN:?set RUN to the runs/ script that launches this}"
: "${TAG:?set TAG to the git tag that names the commit of this run}"
SCANS=${1:-}
VM_TYPE=${VM_TYPE:-c6i.4xlarge}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)

if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/microns pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/microns/ or the lockfile; commit them first" >&2
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
git -C "$ROOT" tag -a "$TAG" -m "MICrONS phase 2: the code $RUN ran" "$SHA"
git -C "$ROOT" push -q origin "refs/tags/$TAG"
NAME="microns-processed-$(date -u +%m%d-%H%M%S)"

STAGE=$(mktemp -d)
(cd "$ROOT" && uv build --wheel -q --out-dir "$STAGE")
# The job installs exactly the versions uv.lock resolves for this ingest, git
# sources (VirtualiZarr, shadowzarr) included, and the wheel without its own
# dependencies.
(cd "$ROOT" && uv export --package microns-ingest --no-hashes --no-emit-workspace \
    --no-dev --frozen -q -o "$STAGE/requirements.txt")
cp "$ROOT"/ingests/microns/{ingest.py,plumbing.py,processed.py,verify_processed.py} \
    "$HERE/run_processed.sh" "$STAGE/"
echo "$SHA" > "$STAGE/COMMIT"
echo "$RUN ($TAG)" > "$STAGE/RUN"
echo "staged commit $SHA, tagged $TAG: $(ls "$STAGE" | tr '\n' ' ')"

cd "$STAGE"
# coiled batch, not `coiled run --detach`: a detached run stops about 20 s
# after its launching session disconnects. The whole staging directory is one
# --upload, because coiled batch keeps only the last. us-east-2 holds DANDI's
# bucket, whose index nodes and masks the job reads; BossDB's bucket is only
# listed.
coiled batch run --name "$NAME" --region us-east-2 \
    --vm-type "$VM_TYPE" --container ghcr.io/astral-sh/uv:python3.12-bookworm \
    --upload "$STAGE" \
    --secret-env "ARRAYLAKE_TOKEN=$ARRAYLAKE_TOKEN" \
    run_processed.sh "$SCANS"
echo "job $NAME: follow with coiled batch status and coiled batch logs"
