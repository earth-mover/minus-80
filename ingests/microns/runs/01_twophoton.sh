#!/usr/bin/env bash
# Run 1: both TIFF exports of all 19 scans, committed to main and tagged v1
# and v2, then verify.py.
#
# The repo's one virtual chunk container is
# s3://bossdb-open-data/iarpa_microns/minnie/functional_data/two_photon_functional_scans/.
#
# Ran 2026-11-20 as Coiled batch job microns-twophoton-1120-050500 (cluster
# 2412025), c6i.4xlarge (16 cores, 30 GB) in us-east-1, from commit 0cba7b0.
# - NWB metadata of 19 scans read in 79 s.
# - v1 export committed as KV69Y4ABGKVQSQKZBDGG, v2 export as
#   PZK7W7CJ181GTDPC1JFG; ingest 05:06:54 to 05:14:11 UTC (7 min 17 s).
# - verify.py passed in 18 s through both exports, then tagged
#   v1 = KV69Y4ABGKVQSQKZBDGG and v2 = PZK7W7CJ181GTDPC1JFG.
# - Job start to end: about 8 minutes.
set -euo pipefail
bash "$(dirname "$0")/../coiled/launch.sh" arraylake://earthmover-biology/MICrONS
