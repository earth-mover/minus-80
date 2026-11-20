#!/usr/bin/env bash
# The Coiled job that launch.sh starts: copy every unit, then check a sample.
# ARRAYLAKE_TOKEN must be set in the environment.
set -euo pipefail
TARGET=arraylake://earthmover-biology/camelyon
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
echo "commit $(cat COMMIT), run ${RUN:=$(cat RUN)}"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

uv venv -q --python 3.12 .venv
# A wheel cannot carry the git source the workspace pins.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[tiff]" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61" \
    "arraylake>=1.7.0"

export PYTHONUNBUFFERED=1 RUST_LOG=error MINUS80_S3_ANONYMOUS=1
.venv/bin/python -X faulthandler transfer.py --target "$TARGET"
.venv/bin/python verify_transfer.py --target "$TARGET" --slides 8
echo "end $(date -u +%FT%TZ)"
