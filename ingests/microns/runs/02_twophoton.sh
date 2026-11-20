#!/usr/bin/env bash
# Run 2: both TIFF exports of all 19 scans again, into the purged repo, in the
# layout that replaces run 1's.
#
# Run 1 wrote the root as a bioformats2raw.layout collection with an OME
# group listing all 144 field images. That list repeated the hierarchy
# beside it, so the store no longer has it: the root and two-photon are
# plain groups with descriptions, and each scan group is an OME-Zarr 0.6
# scene that names its field images by path. Everything else is as in run 1.
# earthmover-biology/MICrONS is purged before this run, so both commits and
# both tags start fresh; verify.py tags them v1 and v2 only if it passes.
#
# Not yet run. It waits on Ian's go-ahead and the purge. When it runs, record
# here the date, the VM, the commit it ran from, both snapshots and the
# verify result.
set -euo pipefail
bash "$(dirname "$0")/../coiled/launch.sh" arraylake://earthmover-biology/MICrONS
