#!/usr/bin/env bash
# Run 1: CI04_01 in full.
#
# The published store held CI04_01 only for the recordings around four events
# in November 2018. This run scanned all 469 recordings (216.7 GB) and
# replaced the deployment's group on main.
#
# Ran 2026-11-09 as Coiled job sanctsound-ci04-01-221947 on a c6in.2xlarge,
# launched one second after commit a00d7fe. The launcher then did not check
# for uncommitted changes, so that commit is the closest record of the code.
# Result: snapshot RZ3E76HBREW57HX18PS0. The scan ran at about 330 MB/s, the
# calibration offset came out at 85.391 dB, the published value, and verify
# passed 12 of 12 checks.
set -euo pipefail
VM_TYPE=c6in.2xlarge bash "$(dirname "$0")/../coiled/launch.sh" CI04_01
