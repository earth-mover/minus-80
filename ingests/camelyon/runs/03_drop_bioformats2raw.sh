#!/usr/bin/env bash
# Run 3: remove the bioformats2raw layout that run 2 wrote into CAMELYON17 of
# earthmover-biology/camelyon: the CAMELYON17/OME group and the
# bioformats2raw.layout key, in one commit on main. CAMELYON16 is untouched.
#
# Not run yet. It changes only metadata, so it runs from a laptop, not
# Coiled, and a second run commits nothing. It runs the committed code and
# refuses uncommitted changes, like the Coiled launcher.
set -euo pipefail
: "${ARRAYLAKE_TOKEN:?set ARRAYLAKE_TOKEN to a token with write access to earthmover-biology/camelyon}"
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/camelyon pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src/, ingests/camelyon/ or the lockfile; commit them first" >&2
    exit 1
fi
echo "commit $(git -C "$ROOT" rev-parse HEAD), run ingests/camelyon/runs/03_drop_bioformats2raw.sh"
cd "$ROOT"
uv run --package camelyon-ingest python ingests/camelyon/drop_bioformats2raw.py \
    --repo arraylake://earthmover-biology/camelyon
