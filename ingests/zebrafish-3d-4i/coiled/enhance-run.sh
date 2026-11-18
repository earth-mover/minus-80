#!/usr/bin/env bash
# The Coiled job that enhance-launch.sh starts: run one enhance/ script.
#   bash enhance-run.sh <script.py> [args...]
# ARRAYLAKE_TOKEN must be set in the environment.
# Expected times: install under 2 min; rebuild_levels.py under 90 min (hard
# limit 150 min), printing a progress line every 30 s.
set -euo pipefail
SCRIPT=$1; shift
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
export MINUS80_CODE_COMMIT=$(cat COMMIT)
echo "commit $MINUS80_CODE_COMMIT, script $SCRIPT $*"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
# A wheel cannot carry the git source the workspace pins for VirtualiZarr.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[fractal,zip]" "arraylake>=1.7.0" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61"
echo "$(now) install: done"

( while true; do
    echo "$(now) mem: $(free -m | awk '/Mem/ {print "used " $3 " MB, available " $7 " MB"}'); load $(cut -d' ' -f1 /proc/loadavg)"
    sleep 30
  done ) &
watcher=$!
trap 'kill $watcher 2>/dev/null' EXIT
echo "$(now) $SCRIPT: start"
status=0
timeout 150m .venv/bin/python -X faulthandler -u "$SCRIPT" "$@" || status=$?
echo "$(now) $SCRIPT: done, exit code $status"
exit "$status"
