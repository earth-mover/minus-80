#!/usr/bin/env bash
# The Coiled job that launch_processed.sh starts: phase 2 of the MICrONS
# ingest, then its store check, which tags the processed commit and each CAVE
# version's commit only if every check passes. ARRAYLAKE_TOKEN must be set.
#   bash run_processed.sh [scan,scan,...]
# Expected times, from the laptop trial of scan 4-7 (a VM beside DANDI should
# be faster): install under 2 min; the processed commit about 5 min for 19
# scans, 8 at a time (each scan maps its NWB file in about 60 s and reads its
# masks in about 10 s); the eight CAVE commits about 1 min each; verify about
# 2 min, most of it fetching the CAVE dumps again. Hard limits: 90 min for the
# ingest, 20 min for verify.
set -euo pipefail
REPO=arraylake://earthmover-biology/MICrONS
SCANS=${1:-}
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
echo "commit $(cat COMMIT), run $(cat RUN), scans ${SCANS:-all}"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
uv pip install -q --python .venv -r requirements.txt
uv pip install -q --python .venv --no-deps "./$(ls minus_80-*.whl)"
echo "$(now) install: done"

export PYTHONUNBUFFERED=1 RUST_LOG=error MINUS80_S3_ANONYMOUS=1
# Memory from its own loop, so a job that stalls inside one call still reports.
(while sleep 30; do echo "$(now) memory used $(free -m | awk '/Mem/ {print $3}') MB"; done) &
watcher=$!
trap 'kill $watcher 2>/dev/null' EXIT

echo "$(now) ingest: start"
timeout 90m .venv/bin/python -X faulthandler -W ignore processed.py --repo "$REPO" \
    ${SCANS:+--scans "$SCANS"} --parallel 8
echo "$(now) ingest: done"
echo "$(now) verify: start"
timeout 20m .venv/bin/python -W ignore verify_processed.py --repo "$REPO" ${SCANS:+--scans "$SCANS"} --tag
echo "$(now) verify: done; end $(date -u +%FT%TZ)"
