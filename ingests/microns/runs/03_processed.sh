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
# Ran 2026-11-20 as Coiled batch job microns-processed-1120-215350 (cluster
# 2416423), c6i.4xlarge (16 cores, 32 GiB) in us-east-2, from commit dd82e26,
# tagged microns-run3-processed; 21:56:20 to 22:21:26 UTC (25 min 6 s), exit 0.
# - Install: 6 s.
# - Processed data of 19 scans, 8 at a time: 5 min; 19 forks merged in 1.9 s;
#   committed from 22:02:02 as 9WSAVP0Y5CW36T6RWYHG.
# - One commit per CAVE version (tables, arrays changed, snapshot):
#   v343   2  112  285YSY0EKJ76F0GS5B00
#   v661   2  308  1KB357RG33PM8DZZD51G
#   v795   2   34  RE40KPQ44VT3FVZDEXKG
#   v943   5  618  6Q1MP8C8M8QKHET7TH4G
#   v1078  5   87  VHHM772E04MC2PG85BX0
#   v1181  4   89  T89VWF6FZJNQA6V2V39G
#   v1412  4  376  1BK2EBYDJ4KK0ZWCF0DG
#   v1507  4   72  WPVBN1EHGEFGZSA1FPQ0 (main)
#   The ingest took 1,131 s in all.
# - verify_processed.py passed in 331 s (complete, codecs, images, sources,
#   matches, versions) and tagged processed and cave-v343 ... cave-v1507.
# - Peak memory about 8.7 GB during the ingest, 2 GB during verify.
set -euo pipefail
RUN=ingests/microns/runs/03_processed.sh TAG=microns-run3-processed \
    bash "$(dirname "$0")/../coiled/launch_processed.sh"
