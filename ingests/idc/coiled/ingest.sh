#!/usr/bin/env bash
# The Coiled job launch.sh starts: write COLLECTION's slides into TARGET, then
# check the store. ARRAYLAKE_TOKEN, COLLECTION and TARGET must be set.
set -euo pipefail
: "${COLLECTION:?}" "${TARGET:?}"
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
echo "commit $(cat COMMIT), run ${RUN:=$(cat RUN)}"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

uv venv -q --python 3.12 .venv
# A wheel cannot carry the git source the workspace pins.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[dicom]" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61" \
    "arraylake>=1.7.0" "idc-index>=0.12" "pyarrow>=17"

export PYTHONUNBUFFERED=1 RUST_LOG=error MINUS80_S3_ANONYMOUS=1
# Memory from its own loop, so a job that stalls inside one call still reports.
(while sleep 60; do echo "$(date -u +%T) memory used $(free -g | awk '/Mem/ {print $3}') GB"; done) &
# Four processes per core: a slide's reads wait on the bucket more than its parse runs.
.venv/bin/python -X faulthandler ingest.py "$TARGET" --collection "$COLLECTION" --workers $(( 4 * $(nproc --all) ))
.venv/bin/python verify.py "$TARGET" --collection "$COLLECTION" --sample 10
echo "end $(date -u +%FT%TZ)"
