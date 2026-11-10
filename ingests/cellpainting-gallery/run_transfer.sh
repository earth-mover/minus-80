#!/usr/bin/env bash
# The Coiled job: copy the images, check every plate, then copy the tables.
# Runs from the unpacked bundle that launch_transfer.sh uploads.
set -euo pipefail
TARGET=arraylake://earthmover-biology/cellpainting-gallery
tar xzf bundle.tgz
export PYTHONPATH="$PWD/src:$PWD/ingests/cellpainting-gallery" PYTHONUNBUFFERED=1 RUST_LOG=error
python ingests/cellpainting-gallery/transfer.py --target "$TARGET"
MINUS80_S3_ANONYMOUS=1 python ingests/cellpainting-gallery/verify_transfer.py --target "$TARGET" --sample 20
python ingests/cellpainting-gallery/transfer_tables.py --catalog arraylake:earthmover-biology --repo "$TARGET"
