#!/usr/bin/env bash
# Run 2: finish the ten phase-1 sessions that run 1 started.
#
# Run 1 committed session 1 and stopped on session 2 (runs/01_ten_sessions.sh).
# This run fixes what stopped it: under_group moves shadowzarr's gen entries
# (contiguous NWB datasets) under the session group, a 2022 sort's
# clusters.mlapdv has its axis named and its pickled
# clusters.brainLocationAcronyms_ccf_2017 is left out, and run.sh kills its
# memory loop on exit, so a failed job ends and its log arrives. The job
# resumes: the 2022 pass skips session 1, which main already holds, and writes
# the other nine; then the 2024 pass, the tags and verify.py, as in run 1.
#
# Checked before launch, from a laptop: shadowzarr mapped all 20 NWB files of
# the ten sessions (3 to 15 s each), and under_group, name_dimensions and the
# codec check passed on every one; 7 raw files have gen entries, all moved.
# The headers of all 780 ALF files of both sorts were read; the two new kinds
# above are the only ones. (A full local run of both passes and verify.py over
# the ten sessions had also passed: 311 s, 126 s and 17 s.)
#
# Not yet run.
set -euo pipefail
RUN=ingests/ibl-brain-wide-map/runs/02_resume_ten_sessions.sh TAG=ibl-run2-resume \
    bash "$(dirname "$0")/../coiled/launch.sh"
