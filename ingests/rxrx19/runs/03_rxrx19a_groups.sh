#!/usr/bin/env bash
# Run 3: every group of RxRx19a rewritten on main of earthmover-biology/RxRx19,
# for the display windows, the channel labels and cxsmiles (PLAN.md decisions
# 10-12).
#
# Rewrites the root, dataset, experiment, plate, row, well and site groups'
# documents in one commit on top of run 2's store. It leaves the arrays and
# their packed references alone. Before writing, the ingest decodes 8 Mock-well
# sites per plate for the windows, or reads them from the cache. verify.py
# then checks the store.
#
# Needs ARRAYLAKE_TOKEN.
set -euo pipefail
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/rxrx19 pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src, ingests/rxrx19 or the lockfile; commit them so this run names its code" >&2
    exit 1
fi
echo "run 03_rxrx19a_groups from commit $(git -C "$ROOT" rev-parse --short HEAD), $(date -u +%FT%TZ)"
cd "$ROOT"
time uv run --package rxrx19-ingest python ingests/rxrx19/ingest.py \
    --repo arraylake://earthmover-biology/RxRx19 --dataset rxrx19a --branch main --attributes-only
time uv run --package rxrx19-ingest python ingests/rxrx19/verify.py \
    --repo arraylake://earthmover-biology/RxRx19 --dataset rxrx19a --branch main --sites 10
