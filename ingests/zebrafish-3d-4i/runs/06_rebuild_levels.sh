#!/usr/bin/env bash
# Run 6: rebuild every image's levels 1 to 3 from level 0 (PLAN.md decision 19).
#
# Reads level 0 of all 211 images once, about 390 GB compressed, and writes
# about 133 GB of new coarse levels into one session on the branch enhance,
# committed once after its checks pass. It runs on one Coiled VM in
# eu-west-2, beside the store.
set -euo pipefail
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN}"
bash "$(dirname "$0")/../coiled/enhance-launch.sh" rebuild_levels.py --workers 48
