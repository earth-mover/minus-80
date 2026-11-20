# CAMELYON transfer

Copies the Arraylake repo `earthmover-public/camelyon` into
`earthmover-biology/camelyon`. The repo holds the CAMELYON16 challenge's 399
whole-slide images as OME-Zarr multiscale images. Their pixels are references
to the 512 by 512 JPEG tiles in `s3://camelyon-dataset/` (CC0), and the tumor
outlines, documents and a few coarse derived mask levels are stored in the repo.
The ingest that built the source repo, and its layout notes, are in the
bio-virtualization repository under `ingest-camelyon-arraylake/`.

Only the head of `main` is copied, without history, so the objects that earlier
snapshots left behind in the source are not. The target is meant to receive
CAMELYON17 later, which is why it is named `camelyon`.

| File | What it does |
| --- | --- |
| `transfer.py` | Copies `OME`, `documents` and each slide group below `CAMELYON16` with `minus80.core.repo_copy`, 50 slides per commit. Rerunning skips what the target already holds. |
| `verify_transfer.py` | Compares `OME` and `documents` in full, then a sample of slides: every `zarr.json`, the references or bytes of a sample of arrays, and one decoded full-resolution tile per slide. |
| `coiled/` | Launches both on one Coiled VM in us-west-2, the bucket's region. |
| `runs/01_transfer.sh` | The launch of the real copy, with the commit and result recorded in its header. |

Reading the source needs a token that can read `earthmover-public/camelyon`.
Writing the target needs `earthmover-biology/camelyon` to exist with a virtual
chunk container for `s3://camelyon-dataset/`. `transfer.py` accepts a local
directory as `--target` for a trial copy of a few slides:

    uv run --package camelyon-ingest python ingests/camelyon/transfer.py \
        --target <dir> --only OME,documents,tumor_056
    uv run --package camelyon-ingest python ingests/camelyon/verify_transfer.py \
        --target <dir> --only tumor_056 --slides 1
