#!/usr/bin/env bash
# Run 1: write every GTEx slide in IDC (release v25, 25,503 slides) into
# earthmover-biology/idc-gtex, each pyramid level, thumbnail and overview
# referenced in place in s3://idc-open-data and pinned to its object's ETag.
#
# Estimated (PLAN.md, "GTEx, ready to run"): indexing faster than the
# laptop's 10.8 slides a second, 51 commits of 500 slides at 10 to 20 s each,
# about an hour in all, under $5. A count that stops moving for two minutes,
# a commit past a minute, or a run past 90 minutes is investigated from
# outside.
#
# Not yet run. Needs, first: the repo, its virtual chunk container for
# s3://idc-open-data/ (anonymous), and a write grant for the ingest token.
# Afterwards, from a laptop with core-drill: verify.py --pins on the repo.
set -euo pipefail
RUN=ingests/idc/runs/01_gtex.sh COLLECTION=gtex TARGET=arraylake://earthmover-biology/idc-gtex \
    VM_TYPE=c6in.4xlarge bash "$(dirname "$0")/../coiled/launch.sh"
