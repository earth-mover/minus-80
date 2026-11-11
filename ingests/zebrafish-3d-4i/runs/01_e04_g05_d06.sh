#!/usr/bin/env bash
# Run 1: E/04, G/05 and D/06, the first wells of the copy.
#
# Ran 2026-10-29 from 05:11 UTC on one m6i.4xlarge in eu-west-2, into the
# branch trial-stream, from the commit "Heartbeat: drop raw pixel bytes and
# say the decoded figure includes the run being decoded". The launcher then
# did not check for uncommitted changes, so that commit is the closest record
# of the code. Result: E/04 and G/05 committed, at 18.0 and 17.5 s per GB of
# zip read. D/06 failed on a member that did not inflate, after committing 41
# channel runs; run 2 resumed it.
set -euo pipefail
bash "$(dirname "$0")/../coiled/launch.sh" trial-stream E/04,G/05,D/06
