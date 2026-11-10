#!/usr/bin/env bash
# Run 2: CI02_01 and CI05_01 in full.
#
# The published store held both deployments only for the recordings around
# four events in November 2018. This run scans all of their recordings,
# 590 files (305.3 GB) for CI02_01 and 755 files (369.0 GB) for CI05_01, and
# replaces each deployment's group on main. Both calibration certificates
# are transcribed in ingest.CERTIFICATES.
set -euo pipefail
RUN=ingests/sanctsound/runs/02_ci02_01_ci05_01.sh bash "$(dirname "$0")/../coiled/launch.sh" CI02_01 CI05_01
