#!/usr/bin/env bash
# Run 5: add labels/missing_source to every image (PLAN.md decision 18).
#
# Builds the labels from each image's lists of damaged source chunks and
# reads no source bytes. It writes 383 shards that are mostly zeros, so it
# runs from any machine with the ingest token, on the branch enhance.
set -euo pipefail
cd "$(dirname "$0")/../../.."
uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/enhance/missing_source.py
