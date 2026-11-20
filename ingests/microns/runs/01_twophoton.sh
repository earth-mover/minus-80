#!/usr/bin/env bash
# Run 1: both TIFF exports of all 19 scans, committed to main and tagged v1
# and v2, then verify.py.
#
# Not yet run. It waits on PLAN.md's approval and on write access to
# earthmover-biology/MICrONS, whose one virtual chunk container is
# s3://bossdb-open-data/iarpa_microns/minnie/functional_data/two_photon_functional_scans/.
# When it runs, record here the date, the VM, the commit it ran from, both
# snapshots and the verify result.
set -euo pipefail
bash "$(dirname "$0")/../coiled/launch.sh" arraylake://earthmover-biology/MICrONS
