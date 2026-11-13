#!/usr/bin/env bash
# Run 2: CI02_01 and CI05_01 in full.
#
# The published store held both deployments only for the recordings around
# four events in November 2018. This run scans all of their recordings,
# 590 files (305.3 GB) for CI02_01 and 755 files (369.0 GB) for CI05_01, and
# replaces each deployment's group on main. Both calibration certificates
# are transcribed in ingest.CERTIFICATES.
#
# Ran 2026-11-10 as Coiled job 2358794 on a c6in.4xlarge, from commit d617fca,
# in 10 min 50 s.
#   CI02_01: snapshot FTNXNZZBFPZNAK1ETBVG. Scan 2 min 35 s, about 1.97 GB/s.
#            Calibration 86.291 dB, the value the store held before.
#   CI05_01: snapshot 8TMM27Z42J3YBQ32HH40. Scan 3 min 10 s, about 1.94 GB/s.
#            Calibration 85.891 dB, the value the store held before.
# verify.py found every sampled segment identical to the source for both.
set -euo pipefail
RUN=ingests/sanctsound/runs/02_ci02_01_ci05_01.sh bash "$(dirname "$0")/../coiled/launch.sh" CI02_01 CI05_01
