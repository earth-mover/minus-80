#!/usr/bin/env bash
# Run 3: move each coarse label level onto the pixel it keeps (PLAN.md decision 17).
#
# Metadata only, so it runs from any machine with the ingest token; it writes
# nothing but the label groups' attributes.
#
# Ran 2026-11-02 at 16:08 UTC from a laptop, from git commit 1a28660, into
# the branch enhance, which it started at source-copy. Snapshot
# 0BP1BZ978RVDNE19W4Q0.
set -euo pipefail
cd "$(dirname "$0")/../../.."
uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/enhance/label_translations.py
