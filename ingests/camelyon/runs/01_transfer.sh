#!/usr/bin/env bash
# Run 1: copy the head of earthmover-public/camelyon into earthmover-biology/camelyon.
#
# The job runs the committed code that HEAD names when it launches; the
# launcher refuses uncommitted changes. Record the Coiled job, commit,
# snapshot and timings here once it has run.
set -euo pipefail
RUN=ingests/camelyon/runs/01_transfer.sh bash "$(dirname "$0")/../coiled/launch.sh"
