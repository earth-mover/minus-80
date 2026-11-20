#!/usr/bin/env bash
# Run 3: phase 2 of PLAN-phase2.md into earthmover-biology/MICrONS.
#
# Each of the 19 scans' DANDI 000402 NWB files whole at
# two-photon/<scan>/nwb (unified NWB Zarr v3 convention), its ROI masks as
# rois-<n> images and its unit axis, as one commit; then one commit per CAVE
# version with a dump of the match tables (v343, v661, v795, v943, v1078,
# v1181, v1412, v1507); then verify_processed.py, which tags them processed
# and cave-v<version> if every check passes. The launcher refuses uncommitted
# or unpushed code and tags the run's commit microns-run3-processed.
#
# Not yet run. Needs, from Ian: approval of PLAN-phase2.md, and the repo's
# virtual chunk container for s3://dandiarchive/blobs/ (PLAN-phase2.md,
# "Arraylake setup").
set -euo pipefail
RUN=ingests/microns/runs/03_processed.sh TAG=microns-run3-processed \
    bash "$(dirname "$0")/../coiled/launch_processed.sh"
