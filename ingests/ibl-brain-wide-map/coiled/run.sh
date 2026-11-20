#!/usr/bin/env bash
# The Coiled job that launch.sh starts: write the phase-1 sessions with the
# 2022 sort and tag sort-2022, write the 2024 sort and tag sort-2024, then run
# the store check. ARRAYLAKE_TOKEN must be set in the environment.
# Expected times: install under 2 min; the 2022 pass about 30 s a session
# (hard limit 40 min); the 2024 pass about 10 s a session (hard limit 20 min);
# verify about 15 s (hard limit 5 min). A session that is already on main
# with the pass's sort is skipped, so a stopped job can be run again.
set -euo pipefail
REPO=arraylake://earthmover-biology/ibl-brain-wide-map
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
echo "commit $(cat COMMIT), run $(cat RUN)"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
# A wheel cannot carry the git sources the workspace pins.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[nwb]" "arraylake>=1.7.0" pandas pyarrow xarray \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61" \
    "shadowzarr @ git+https://github.com/bendichter/zindi@07e6f65938ef30400251f09b29323832a25fda88"
echo "$(now) install: done"

export PYTHONUNBUFFERED=1 RUST_LOG=error MINUS80_S3_ANONYMOUS=1
# Memory from its own loop, so a job that stalls inside one call still reports.
(while sleep 60; do echo "$(now) memory used $(free -m | awk '/Mem/ {print $3}') MB"; done) &

echo "$(now) 2022 pass: start"
timeout 40m .venv/bin/python -X faulthandler -W ignore ingest.py --repo "$REPO" --sort 2022 --tag sort-2022
echo "$(now) 2022 pass: done"
echo "$(now) 2024 pass: start"
timeout 20m .venv/bin/python -X faulthandler -W ignore ingest.py --repo "$REPO" --sort 2024 --sorting-only --tag sort-2024
echo "$(now) 2024 pass: done"
echo "$(now) verify: start"
timeout 5m .venv/bin/python -W ignore verify.py --repo "$REPO"
echo "$(now) verify: done"
echo "end $(date -u +%FT%TZ)"
