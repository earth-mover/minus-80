#!/usr/bin/env bash
# Run 1: write every GTEx H&E slide in IDC (release v25: 25,503 slides from
# 971 donors) into earthmover-biology/idc-gtex. Each level, thumbnail and
# overview reads its tiles from s3://idc-open-data, pinned to the objects'
# ETags.
#
# Estimated (PLAN.md, "GTEx, ready to run"): indexing faster than the
# laptop's 7 to 9 slides a second, 103 commits of 250 slides at 10 to 20 s
# each, about an hour in all, under $5.
#
# Watch list, each investigated from outside rather than waited out:
# - a slide count that stops moving for two minutes;
# - a commit past a minute. The local benchmark's slow commits had outside
#   causes: laptop sleep (98 to 823 s) and CPU shared with other jobs (32 to
#   34 s, CPU time equal to wall time); otherwise commits of 500 slides took
#   a median 10.7 s, 90th percentile 19.5 s. Arraylake adds its latency per
#   manifest;
# - peak memory past 24 GB of the VM's 32 GiB (the batch line and the memory
#   loop both report it);
# - a run past 90 minutes.
#
# Not yet run. Arraylake is set up (2026-11-20): the bucket config
# idc-open-data, its public access policy, and the repo
# earthmover-biology/idc-gtex with the container s3://idc-open-data/, and
# the ingest token's write grant on idc-gtex.
#
# After the job, from a laptop with core-drill: verify.py --pins on the
# repo; then tag the verified snapshot idc-v25 in Icechunk, push the git tag
# idc-run1-gtex on this script's commit, and fill in the result here.
set -euo pipefail
RUN=ingests/idc/runs/01_gtex.sh COLLECTION=gtex TARGET=arraylake://earthmover-biology/idc-gtex \
    VM_TYPE=c6in.4xlarge bash "$(dirname "$0")/../coiled/launch.sh"
