#!/usr/bin/env bash
# The Coiled job that launch.sh starts with JOB=ingest.sh: add CHALLENGE's
# slides to earthmover-biology/camelyon, then check the result.
# ARRAYLAKE_TOKEN and CHALLENGE must be set in the environment.
set -euo pipefail
: "${CHALLENGE:?set CHALLENGE to CAMELYON16 or CAMELYON17}"
TARGET=arraylake://earthmover-biology/camelyon
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
echo "commit $(cat COMMIT), run ${RUN:=$(cat RUN)}"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

uv venv -q --python 3.12 .venv
# A wheel cannot carry the git source the workspace pins.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[asap,tiff]" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61" \
    "arraylake>=1.7.0"

export PYTHONUNBUFFERED=1 RUST_LOG=error MINUS80_S3_ANONYMOUS=1
# Memory from its own loop, so a job that stalls inside one call still reports.
(while sleep 60; do echo "$(date -u +%T) memory used $(free -g | awk '/Mem/ {print $3}') GB"; done) &
.venv/bin/python -X faulthandler ingest.py --challenge "$CHALLENGE" --repo "$TARGET" --threads 32
.venv/bin/python verify.py --challenge "$CHALLENGE" --repo "$TARGET"
echo "end $(date -u +%FT%TZ)"
