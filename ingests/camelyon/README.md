# CAMELYON

The CAMELYON16 and CAMELYON17 whole-slide images of sentinel lymph nodes, in
one Icechunk store, `earthmover-biology/camelyon`. Each slide is an OME-Zarr
image whose chunks are the 512 × 512 JPEG tiles of its TIFF in
`s3://camelyon-dataset/` (us-west-2, anonymous, CC0), with its reference mask
as a label image, the pathologists' tumor outlines as arrays and QuPath
GeoJSON, and the challenge's labels in the attributes. Only the outlines,
documents and a few derived coarse mask levels are stored in the repo.

The store was built in two stages:

1. **Transfer.** CAMELYON16 was ingested first into `earthmover-public/camelyon`.
   Run 1 copied the head of that repo's `main` into
   `earthmover-biology/camelyon`, without history.
2. **Ingest.** `ingest.py` builds a challenge's slides from the bucket. It is
   the ingest that built CAMELYON16, rebuilt on `minus80`, and it adds
   CAMELYON17 to the same repo, 100 slides per commit.

`LAYOUTS.md` says how the bucket maps onto the store and why. `PLAN.md` holds
the CAMELYON17 plan and its decisions; `diagrams/` draws the source, the
mapping and the store.

| File | What it does |
| --- | --- |
| `ingest.py` | Writes a challenge's slides, each in its own fork of a session on a thread pool, then merges the forks, rewrites the container's tables and commits, 100 slides at a time. A rerun skips the slides already in the store. |
| `plumbing.py` | The source reads, the writes into a session, the repo, and the thread pool with its progress lines. |
| `verify.py` | Checks a store against the bucket in about a minute: completeness and labels, the outlines, a few tiles against tifffile, and the OME-Zarr gates. |
| `fixture.py`, `test_ingest.py` | A small copy of the bucket's layout, and the ingest run end to end on it, with no network. |
| `diagrams.py` | Draws `diagrams/*.svg` from a bucket listing, the header census and built stores. |
| `camelyon.ipynb` | Opens the finished store cold. |
| `transfer.py`, `verify_transfer.py` | Copy `earthmover-public/camelyon` into another repo, and check the copy. |
| `coiled/` | `launch.sh` starts a job on one Coiled VM in us-west-2: `run.sh` (the transfer) or `ingest.sh`. |
| `runs/` | One script per batch that wrote the store, with the commit and result recorded in its header. |

Build a few slides into a local repo, check them, and run the tests:

    uv run --package camelyon-ingest python ingests/camelyon/ingest.py \
        --challenge CAMELYON17 --repo <dir> --slides patient_004_node_4,patient_081_node_0
    uv run --package camelyon-ingest python ingests/camelyon/verify.py \
        --challenge CAMELYON17 --repo <dir>
    uv run --package camelyon-ingest pytest --import-mode=prepend ingests/camelyon

Reading `earthmover-biology/camelyon` needs an Arraylake login or a token with
read access. Writing it needs the repo's virtual chunk container for
`s3://camelyon-dataset/`, which it has.
