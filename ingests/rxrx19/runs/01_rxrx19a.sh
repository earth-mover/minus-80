#!/usr/bin/env bash
# Run 1: RxRx19a in full, onto main of earthmover-biology/RxRx19.
#
# All 57 plates, their 1,527,600 PNGs referenced in place as one manifest per
# plate (ice-pack), and the embeddings copied into a float32 array. Building
# reads no image data, so the run is local rather than on Coiled: the trial
# wrote 10 plates in 17 s. verify.py then checks the store.
#
# Needs ARRAYLAKE_TOKEN (the ingest client, granted the repo) and ice-pack on
# PATH or in ICE_PACK.
set -euo pipefail
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
if [[ -n $(git -C "$ROOT" status --porcelain -- src ingests/rxrx19 pyproject.toml uv.lock) ]]; then
    echo "uncommitted changes under src, ingests/rxrx19 or the lockfile; commit them so this run names its code" >&2
    exit 1
fi
echo "run 01_rxrx19a from commit $(git -C "$ROOT" rev-parse --short HEAD), $(date -u +%FT%TZ)"
cd "$ROOT"
time uv run --package rxrx19-ingest python ingests/rxrx19/ingest.py \
    --repo arraylake://earthmover-biology/RxRx19 --dataset rxrx19a --branch main --packed --embeddings
time uv run --package rxrx19-ingest python ingests/rxrx19/verify.py \
    --repo arraylake://earthmover-biology/RxRx19 --dataset rxrx19a --branch main --sites 10
