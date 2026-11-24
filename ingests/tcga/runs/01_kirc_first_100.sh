#!/usr/bin/env bash
# Run 1: the DX1 slides of the first 100 TCGA-KIRC patients, in barcode order,
# from IDC v25, onto main of earthmover-biology/tcga.
set -euo pipefail
bash "$(dirname "$0")/../coiled/launch.sh" tcga_kirc 100 main
