#!/usr/bin/env bash
# The Coiled job that launch.sh starts: write the phase-1 sessions with the
# 2022 sort and tag sort-2022, write the 2024 sort and tag sort-2024, then run
# the store check. ARRAYLAKE_TOKEN must be set in the environment.
# Expected times: install under 2 min; the 2022 pass about 30 s a session
# (hard limit 40 min); the 2024 pass about 10 s a session (hard limit 20 min);
# verify under 1 min (hard limit 5 min).
#
# A relaunch resumes. A pass whose tag (sort-2022, sort-2024) is already in the
# repo is skipped whole. Inside a pass, a session is skipped when main already
# holds it with that pass's sort or a later one, so a relaunch never writes the
# 2022 sort over the 2024 one, and never writes a session's NWB files twice. A
# pass tags main only when every session holds exactly its sort.
set -euo pipefail
REPO=arraylake://earthmover-biology/ibl-brain-wide-map
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
echo "commit $(cat COMMIT), run $(cat RUN)"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
uv pip install -q --python .venv -r requirements.txt
uv pip install -q --python .venv --no-deps "./$(ls minus_80-*.whl)"
echo "$(now) install: done"

export PYTHONUNBUFFERED=1 RUST_LOG=error MINUS80_S3_ANONYMOUS=1
# Memory from its own loop, so a job that stalls inside one call still reports.
# The loop is killed when the script exits, however it exits: left running, it
# holds the job's output open, so the job never ends and its log never arrives.
(while sleep 60; do echo "$(now) memory used $(free -m | awk '/Mem/ {print $3}') MB"; done) &
watcher=$!
trap 'echo "$(now) run.sh: exiting with status $?"; kill $watcher 2>/dev/null' EXIT

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
