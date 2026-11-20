#!/usr/bin/env bash
# Run 1: every allowlisted study (56: CC BY 4.0 and CC0 1.0), one VM in
# us-west-2, into the branch ingest of earthmover-biology/chammi-75.
#
# Not yet run. When it runs, record here the UTC start and end, the commit
# it ran from (the job prints it), the snapshot it committed and what
# verify.py reported.
set -euo pipefail
bash "$(dirname "$0")/../coiled/launch.sh" ingest
