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
#
# Ran 2026-11-11 from 23:57 UTC on a laptop, from commit 1bb67cf, into main.
#   ingest: 2 min 22 s. 57 plates, 689,193 nodes, 1,527,600 references built
#           in 7.2 s; the embeddings (1.4 GB deflated, 64 MiB ranges from
#           GCS at 20-27 s each) parsed at about 15,000 rows/s; nodes and
#           embeddings committed as RZ95RTP7HPTAM0Z5R4VG at 129.5 s; ice-pack
#           wrote 57 manifests and committed MTX2EWEF078HBAMDP0VG at 141.2 s.
#   verify: pass in 2 min 30 s. 57 plates' wells match metadata.csv; a plate,
#           a well and a site validate; 10 sites equal their PNGs. The plate
#           check took 108 s: each node lookup in this snapshot took about 1 s.
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
