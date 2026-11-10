#!/usr/bin/env bash
# The Coiled job that launch.sh starts: scan, build and verify each deployment
# in turn, committing each to main.
#   bash run.sh <deployment>...
# ARRAYLAKE_TOKEN must be set in the environment.
# Expected times for CI04_01's 217 GB: install under 3 min (pyFLAC compiles),
# scan about 11 min on a c6in.2xlarge (hard limit 60 min), build under 10 min
# including the calibration measurement (hard limit 30 min), verify under 2 min.
set -euo pipefail
REPO=arraylake://earthmover-biology/sanctsound
# The uploaded directory may land inside the working directory; work where the wheel is.
cd "$(dirname "$(find . -name 'minus_80-*.whl' -print -quit)")"
now() { date -u +%T; }
echo "commit $(cat COMMIT), deployments $*"
echo "host $(hostname), $(nproc --all) cores, $(free -g | awk '/Mem/ {print $2}') GB memory, start $(date -u +%FT%TZ)"

echo "$(now) install: start"
uv venv -q --python 3.12 .venv
# A wheel cannot carry the git sources the workspace pins.
uv pip install -q --python .venv \
    "./$(ls minus_80-*.whl)[flac]" \
    "flac-numcodecs @ git+https://github.com/ianhi/flac-numcodecs@zarr3-flac-codec-2026-10-19" \
    "virtualizarr @ git+https://github.com/ianhi/VirtualiZarr@83da047711558dfb4fc004db45641d28a6687f61" \
    "arraylake>=1.7.0" "h5py>=3.16" pyrsktools scipy
echo "$(now) install: done"

for DEP in "$@"; do
    echo "$(now) scan $DEP: start"
    timeout 60m .venv/bin/python -X faulthandler -u ingest.py scan "$DEP" \
        --cache "./scan/$DEP" --out "./$DEP.npz"
    echo "$(now) scan $DEP: done, chunk table $(stat -c %s "./$DEP.npz") bytes"

    echo "$(now) build $DEP: start"
    timeout 30m .venv/bin/python -X faulthandler -u ingest.py build "$DEP" \
        --table "./$DEP.npz" --repo "$REPO"
    echo "$(now) build $DEP: done"

    echo "$(now) verify $DEP: start"
    timeout 10m .venv/bin/python -u verify.py --repo "$REPO" --deployment "$DEP"
    echo "$(now) verify $DEP: done"
done
echo "end $(date -u +%FT%TZ)"
