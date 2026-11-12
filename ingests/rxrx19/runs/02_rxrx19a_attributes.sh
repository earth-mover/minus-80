#!/usr/bin/env bash
# Run 2: RxRx19a's license, READMEs and provenance, onto main of
# earthmover-biology/RxRx19.
#
# Rewrites only the root and rxrx19a groups' attributes, in their own commit,
# on top of run 1's store: the zip's LICENSE, the attribution, both READMEs,
# the citation, the pixel size's basis, the stain table and the READMEs'
# swapped rows and columns.
#
# Needs ARRAYLAKE_TOKEN.
set -euo pipefail
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/rxrx19 pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src, ingests/rxrx19 or the lockfile; commit them so this run names its code" >&2
    exit 1
fi
echo "run 02_rxrx19a_attributes from commit $(git -C "$ROOT" rev-parse --short HEAD), $(date -u +%FT%TZ)"
cd "$ROOT"
time uv run --package rxrx19-ingest python ingests/rxrx19/ingest.py \
    --repo arraylake://earthmover-biology/RxRx19 --dataset rxrx19a --branch main --attributes-only
