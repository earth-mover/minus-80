#!/usr/bin/env bash
# Run 2: the other 27 wells, and D/06 resumed, seven wells on each of four VMs.
#
# Ran 2026-10-29 from 15:24 to 16:24 UTC on four m6i.4xlarge in eu-west-2,
# into the branch trial-stream, from the same commit as run 1. Every job
# exited 0. The plate then listed 30 wells and 211 images in snapshot
# YFEF3GGBZ2N59YY6T8EG, and main was fast-forwarded to it and tagged
# source-copy.
set -euo pipefail
LAUNCH="$(dirname "$0")/../coiled/launch.sh"
bash "$LAUNCH" trial-stream D/06,B/02,B/05,B/06,B/07,C/02,C/04
bash "$LAUNCH" trial-stream F/05,F/06,F/07,G/02,G/03,G/06,G/07
bash "$LAUNCH" trial-stream E/02,E/03,E/05,E/06,E/07,F/03,F/04
bash "$LAUNCH" trial-stream C/05,C/06,C/07,D/02,D/03,D/05,D/07
