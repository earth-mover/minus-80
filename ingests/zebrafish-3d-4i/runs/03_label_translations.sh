#!/usr/bin/env bash
# Run 3: move each coarse label level onto the pixel it keeps (PLAN.md decision 17).
#
# Metadata only, so it runs from any machine with the ingest token; it writes
# nothing but the label groups' attributes.
#
# A first run on 2026-11-02 cited a plan decision number in its commit
# message; the branch enhance was reset to source-copy and this ran again.
set -euo pipefail
cd "$(dirname "$0")/../../.."
uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/enhance/label_translations.py
