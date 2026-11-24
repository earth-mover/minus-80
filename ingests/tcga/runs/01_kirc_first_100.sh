#!/usr/bin/env bash
# Run 1: the DX1 slides of the first 100 TCGA-KIRC patients, in barcode order,
# from IDC v25, onto main of earthmover-biology/tcga.
#
# Ran 2026-11-24 from 17:17 to 17:22 UTC on one m6i.4xlarge in us-east-1, from
# commit 3dc1be8. The job exited 0 after 100 slides in ten commits, about
# 3 s a slide with 12 workers. main was then at snapshot ZSSTDMJE2YNQQJ4JSSE0.
set -euo pipefail
bash "$(dirname "$0")/../coiled/launch.sh" tcga_kirc 100 main
