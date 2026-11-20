#!/usr/bin/env bash
# The Coiled job launch.sh starts: write COLLECTION's slides into TARGET, then
# check the store. ARRAYLAKE_TOKEN, COLLECTION and TARGET must be set.
set -euo pipefail
: "${COLLECTION:?}" "${TARGET:?}"
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
echo "commit $(cat COMMIT), run ${RUN:=$(cat RUN)}"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

uv venv -q --python 3.12 .venv
uv pip install -q --python .venv -r requirements.txt
uv pip install -q --python .venv --no-deps "./$(ls minus_80-*.whl)"

export PYTHONUNBUFFERED=1 RUST_LOG=error MINUS80_S3_ANONYMOUS=1
# Memory from its own loop, so a job that stalls inside one call still reports.
# The peak is the high-water mark of this loop's samples; ingest.py's batch line
# reports its own process's peak, at the merge.
(peak=0; while sleep 60; do used=$(free -m | awk '/Mem/ {print $3}'); if (( used > peak )); then peak=$used; fi
  echo "$(date -u +%T) memory used ${used} MB, peak ${peak} MB of $(free -m | awk '/Mem/ {print $2}') MB"; done) &
# The loop holds the job's stdout open, so a failure that ends this script
# would otherwise leave the job running with its logs unreleased.
WATCH=$!
trap 'status=$?; kill "$WATCH" 2>/dev/null; echo "exit $status"' EXIT
# Four processes per core: a slide's reads wait on the bucket more than its parse runs.
.venv/bin/python -X faulthandler ingest.py "$TARGET" --collection "$COLLECTION" --workers $(( 4 * $(nproc --all) ))
.venv/bin/python verify.py "$TARGET" --collection "$COLLECTION" --sample 10
echo "end $(date -u +%FT%TZ)"
