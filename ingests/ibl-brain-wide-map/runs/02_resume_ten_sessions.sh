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
# Ran 2026-11-20 as Coiled batch job ibl-bwm-223812 (cluster 2416608),
# c6in.xlarge in us-east-1, from cdc0223 (tag ibl-run2-resume). The job
# started 22:40:03 UTC and ended 22:51:15 with status 0; install took 9 s.
# - 2022 pass, 22:40:12 to 22:48:34 (497 s): session 1 already on main and
#   skipped; sessions 2 to 10 committed, about 55 s each, of which the commit
#   took 3.6 to 4.6 s. Tag sort-2022 at R4C3JACGSHGCB83062AG.
# - 2024 pass, 22:48:34 to 22:50:51 (136 s): all ten sessions, about 13 s
#   each, commits 1.4 to 2.0 s. Tag sort-2024 at K2FPGX52P75CHCJDJXAG, which
#   is main.
# - main: 20 snapshots after the repo's initial one, 7CSTFJXAM55CJG0D0WW0
#   from run 1 and 19 from this run.
# - verify.py: 0 failures in 22 s.
# - Peak memory used on the VM, sampled each minute: 1,757 MB of 7 GB.
set -euo pipefail
RUN=ingests/ibl-brain-wide-map/runs/02_resume_ten_sessions.sh TAG=ibl-run2-resume \
    bash "$(dirname "$0")/../coiled/launch.sh"
