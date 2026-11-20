#!/usr/bin/env bash
# The Coiled job that launch.sh starts: ingest the studies into the branch,
# then run the store check against what was committed.
#   bash run.sh <branch> [study,study,...]
# ARRAYLAKE_TOKEN must be set in the environment.
# Expected times: install under 2 min; inputs (the 3.0 GB metadata CSV and the
# zip's 1.49 GB central directory) under 5 min; the studies about 15 min; the
# merge, tables and commit under 10 min; the check about 2 min. Hard limit 90 min.
set -euo pipefail
BRANCH=$1 STUDIES=${2:-}
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
echo "commit $(cat COMMIT), branch $BRANCH, studies ${STUDIES:-every allowlisted study}"
echo "host $(hostname), $(nproc --all) cores (OMP_NUM_THREADS=${OMP_NUM_THREADS:-unset}), $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
# A wheel cannot carry the git source the workspace pins for VirtualiZarr.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[zip]" "arraylake>=1.7.0" "duckdb>=1.1" "imagecodecs>=2026.1" \
    imagecodecs-zarr "pandas>=2.2" "pillow>=11" "safetensors>=0.4" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61"
echo "$(now) install: done"

# Memory and the largest processes every 15 s, from a loop of its own, so a
# job killed from outside leaves a trace of what it was doing just before.
( while true; do
    echo "$(now) mem: $(free -m | awk '/Mem/ {print "used " $3 " MB, available " $7 " MB"}'); top: $(ps -eo rss=,comm= --sort=-rss | head -3 | awk '{printf "%s %d MB; ", $2, $1/1024}')"
    sleep 15
  done ) &
watcher=$!
trap 'kill $watcher 2>/dev/null' EXIT

REPO=arraylake://earthmover-biology/chammi-75
SELECT=()
[[ -n $STUDIES ]] && SELECT=(--studies "$STUDIES")
echo "$(now) ingest: start"
timeout 75m .venv/bin/python -X faulthandler -u ingest.py --repo "$REPO" --branch "$BRANCH" \
    --work ./work --parallel 8 "${SELECT[@]}"
echo "$(now) ingest: done"

echo "$(now) verify: start"
timeout 15m .venv/bin/python -u verify.py --repo "$REPO" --branch "$BRANCH" --work ./work
echo "$(now) verify: done; end $(date -u +%FT%TZ)"
