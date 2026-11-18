#!/usr/bin/env bash
# Run 4: color each round's channels blue, green, magenta and yellow (PLAN.md decision 16).
#
# Metadata only, so it runs from any machine with the ingest token; it writes
# nothing but the 211 images' attributes, on the branch enhance.
set -euo pipefail
cd "$(dirname "$0")/../../.."
uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/enhance/channel_colors.py
