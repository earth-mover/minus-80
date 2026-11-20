#!/usr/bin/env bash
# Run 2: add the 1,000 CAMELYON17 slides to earthmover-biology/camelyon, on top
# of the CAMELYON16 history that run 1 copied there.
#
# Runs from commit b193fe5, the code that passed review; e4ce76d, which
# launched it, only records this line. Both are kept by the tag
# camelyon-run2-camelyon17, at e4ce76d, since this branch was later rebased. The job runs the committed code that
# HEAD names when it launches, and the launcher refuses uncommitted changes.
#
# Ran 2026-11-20 as Coiled batch job camelyon-ingest-054914 (cluster
# 2412224), c6in.4xlarge in us-west-2, from e4ce76d (tag
# camelyon-run2-camelyon17), 32 threads.
# - Committed two batches of 100 slides, patient_000 to patient_039:
#   3P2EYKBSNNMFNCJMV3S0 and SECKQK7BFZQ8EQ4CB880.
# - About 67 s per batch: parsing about 40 s, commit 24 s.
# - Stopped by Ian after the second batch.
# Resumed from the same commit, e4ce76d, as Coiled batch job
# camelyon-ingest-055457 (cluster 2412241), which skips the 200 slides
# already committed. Its result is recorded here when it lands.
#
# The code at e4ce76d writes each batch's message with the container's
# running total ("...; the container now holds N slides"); later code
# writes only what the commit adds. It also writes CAMELYON17 with the
# bioformats2raw layout, which run 3 removes.
set -euo pipefail
RUN=ingests/camelyon/runs/02_camelyon17.sh JOB=ingest.sh CHALLENGE=CAMELYON17 VM_TYPE=c6in.4xlarge \
    bash "$(dirname "$0")/../coiled/launch.sh"
