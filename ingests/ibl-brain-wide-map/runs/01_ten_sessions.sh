#!/usr/bin/env bash
# Run 1: the ten phase-1 sessions of PLAN.md into earthmover-biology/ibl-brain-wide-map.
#
# Each session's raw and processed NWB files from DANDI 000409 and both
# probes' 2022 spike sorting, a commit per session, tagged sort-2022; then
# both probes' 2024 re-sort, a commit per session, tagged sort-2024; then
# verify.py. The launcher refuses uncommitted or unpushed code and tags the
# run's commit ibl-run1-ten-sessions before the job starts.
#
# Not yet run. Needs, from Ian: the repo, the dandiarchive and
# ibl-brain-wide-map-public bucket configs and access policies, and write
# access for the ingest token (PLAN.md, "Steps").
set -euo pipefail
RUN=ingests/ibl-brain-wide-map/runs/01_ten_sessions.sh TAG=ibl-run1-ten-sessions \
    bash "$(dirname "$0")/../coiled/launch.sh"
