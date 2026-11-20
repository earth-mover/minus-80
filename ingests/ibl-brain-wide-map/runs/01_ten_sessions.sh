#!/usr/bin/env bash
# Run 1: the ten phase-1 sessions of PLAN.md into earthmover-biology/ibl-brain-wide-map.
#
# Each session's raw and processed NWB files from DANDI 000409 and both
# probes' 2022 spike sorting, a commit per session, tagged sort-2022; then
# both probes' 2024 re-sort, a commit per session, tagged sort-2024; then
# verify.py. The launcher refuses uncommitted or unpushed code and tags the
# run's commit ibl-run1-ten-sessions before the job starts.
#
# Launched 2026-11-20 21:51:47 UTC as Coiled batch job ibl-bwm-215013
# (cluster 2416405), c6in.xlarge in us-east-1, from 310cad5 (tag
# ibl-run1-ten-sessions). It committed session 1, sub-ZM-2241, as
# 7CSTFJXAM55CJG0D0WW0 at 21:53:04, then failed on session 2: the raw NWB of
# sub-NR-0020 has contiguous datasets that shadowzarr describes with gen
# entries, which under_group refused. The job did not end, because run.sh's
# memory loop kept its output open, so Coiled showed it running with no logs.
# Run 2 resumes with both fixed.
set -euo pipefail
RUN=ingests/ibl-brain-wide-map/runs/01_ten_sessions.sh TAG=ibl-run1-ten-sessions \
    bash "$(dirname "$0")/../coiled/launch.sh"
