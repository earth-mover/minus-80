#!/usr/bin/env bash
# Run 2: add the 1,000 CAMELYON17 slides to earthmover-biology/camelyon, on top
# of the CAMELYON16 history that run 1 copied there.
#
# Not run yet: it waits on the approval of PLAN.md. The job runs the committed
# code that HEAD names when it launches; the launcher refuses uncommitted
# changes.
#
# Expected, from the laptop trial (PLAN.md, "What has been tested"): ten
# commits of 100 slides each, 15 to 25 minutes on one c6in.4xlarge, then
# verify.py in about a minute.
set -euo pipefail
RUN=ingests/camelyon/runs/02_camelyon17.sh JOB=ingest.sh CHALLENGE=CAMELYON17 VM_TYPE=c6in.4xlarge \
    bash "$(dirname "$0")/../coiled/launch.sh"
