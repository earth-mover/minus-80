#!/usr/bin/env bash
# Run 1: copy the head of earthmover-public/camelyon into earthmover-biology/camelyon.
#
# The job runs the committed code that HEAD names when it launches; the
# launcher refuses uncommitted changes.
#
# Ran 2026-11-20 as Coiled batch job camelyon-transfer-023950 (cluster 2411314),
# c6in.2xlarge in us-west-2, from commit d4c4e69 (tag camelyon-run1-transfer).
# - Source: earthmover-public/camelyon, snapshot J6HF3B2FPE2QW7NCDBB0.
# - Copied all 401 units under CAMELYON16 (399 slides, OME, documents) in nine
#   commits of up to 50 units: about 55 s of copying and 25 s of commit per batch.
# - Target: earthmover-biology/camelyon, main at MNPFXPY2MK0EX78VXRZG.
# - verify_transfer.py --slides 8 passed: documents and sampled references and
#   stored chunks identical, one decoded level-0 tile per slide identical.
# - Wall time from job start to end: 8 min 24 s.
set -euo pipefail
RUN=ingests/camelyon/runs/01_transfer.sh bash "$(dirname "$0")/../coiled/launch.sh"
