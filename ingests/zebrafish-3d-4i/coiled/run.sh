#!/usr/bin/env bash
# The Coiled job that launch.sh starts: convert the given wells, committing
# each to the branch as it is copied.
#   bash run.sh <branch> <well,well,...>
# ARRAYLAKE_TOKEN must be set in the environment.
# Expected times: install under 2 min, branch under 30 s, about 10 min a well
# while EBI keeps up (hard limit 120 min for the job's wells).
set -euo pipefail
BRANCH=$1 WELLS=$2
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
echo "commit $(cat COMMIT), branch $BRANCH, wells $WELLS"
echo "host $(hostname), $(nproc --all) cores (OMP_NUM_THREADS=${OMP_NUM_THREADS:-unset}), $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
# A wheel cannot carry the git source the workspace pins for VirtualiZarr.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[fractal,zip]" "arraylake>=1.7.0" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61"
echo "$(now) install: done"

echo "$(now) branch $BRANCH: start"
.venv/bin/python - "$BRANCH" <<'PY'
import sys
from arraylake import Client
repo = Client().get_repo("earthmover-biology/zebrafish-3d-4i")
if sys.argv[1] not in set(repo.list_branches()):
    # A new branch starts from the repo's first snapshot, so it shares
    # nothing with main but the empty root.
    first = list(repo.ancestry(branch="main"))[-1].id
    repo.create_branch(sys.argv[1], snapshot_id=first)
    print(f"created branch {sys.argv[1]} at {first}")
PY
echo "$(now) branch $BRANCH: done"

echo "$(now) ingest: start"
# Memory and the largest processes every 5 s, from a loop of its own, so a
# job killed from outside leaves a trace of what it was doing just before.
( while true; do
    echo "$(now) mem: $(free -m | awk '/Mem/ {print "used " $3 " MB, available " $7 " MB"}'); top: $(ps -eo rss=,comm= --sort=-rss | head -3 | awk '{printf "%s %d MB; ", $2, $1/1024}')"
    sleep 5
  done ) &
watcher=$!
trap 'echo "$(now) run.sh: exiting (status ${status:-?})"; kill $watcher 2>/dev/null' EXIT
status=0
# Icechunk's debug log goes to a file, whose last lines the ingest prints when
# the copy stops moving; tracebacks still reach the job's own log.
export ICECHUNK_LOG=icechunk=debug
timeout 120m .venv/bin/python -X faulthandler -u ingest.py \
    --repo arraylake://earthmover-biology/zebrafish-3d-4i \
    --branch "$BRANCH" --wells "$WELLS" \
    --directory-cache ./central-directory.parquet --stall-log icechunk.log \
    2> >(sed -u 's/\x1b\[[0-9;]*m//g' | tee -a icechunk.log \
         | grep --line-buffered -v -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T|^ +at |^\s*$' >&2) || status=$?
echo "$(now) icechunk log: $(wc -l < icechunk.log) lines, of which warnings and errors:"
grep -c -E ' (WARN|ERROR) ' icechunk.log || true
echo "$(now) ingest: done, exit code $status"
echo "end $(date -u +%FT%TZ)"
exit "$status"
