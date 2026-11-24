#!/usr/bin/env bash
# The Coiled job that launch.sh starts: ingest the first <limit> slides of a
# TCGA collection, committing every 10 slides.
#   bash run.sh <collection> <limit> <branch>
# ARRAYLAKE_TOKEN must be set in the environment.
# Expected times: install under 2 min, then about 10 to 30 s a slide per
# worker; 100 slides in under 15 min (hard limit 60 min).
set -euo pipefail
COLLECTION=$1 LIMIT=$2 BRANCH=$3
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
echo "commit $(cat COMMIT), collection $COLLECTION, limit $LIMIT, branch $BRANCH"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, $(df -h . | awk 'NR==2 {print $4}') disk free, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
# A wheel cannot carry the git source the workspace pins for VirtualiZarr.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[dicom]" "arraylake>=1.7.0" "idc-index==0.13.5" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61"
echo "$(now) install: done"

( while true; do
    echo "$(now) mem: $(free -m | awk '/Mem/ {print "used " $3 " MB, available " $7 " MB"}'); disk free $(df -h . | awk 'NR==2 {print $4}')"
    sleep 30
  done ) &
watcher=$!
trap 'kill $watcher 2>/dev/null' EXIT

echo "$(now) ingest: start"
export MINUS80_S3_ANONYMOUS=1
status=0
timeout 60m .venv/bin/python -X faulthandler -u ingest.py \
    --repo arraylake://earthmover-biology/tcga --branch "$BRANCH" \
    --collection "$COLLECTION" --limit "$LIMIT" --workers 12 \
    2> >(grep --line-buffered -v -E ' WARN |^\s+at |^\s*$' >&2) || status=$?
echo "$(now) ingest: done, exit code $status"
exit "$status"
