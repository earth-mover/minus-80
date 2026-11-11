# Working in minus-80

Instructions for coding agents and contributors. The README says what the
repository is; this file says how to work in it.

## The goal

**Make large public bioimaging archives usable without copying them.** Every
ingest produces an Icechunk store whose chunks reference the archive's own
bytes, and whose hierarchy a stranger can navigate: an OME-Zarr layout with
real axes, real physical units, resolution levels declared as such, groups that
say what they contain, and the study's annotations travelling with the pixels.
One kind of ingest copies: see Native ingests.

A store that references every byte but exposes one array per file has achieved
nothing a directory listing did not. Judge every output by one test: **open the
store with nothing else for reference. Can you tell what it is?** If the answer
needs the source directory, the original filenames or the person who ran the
ingest, the work is not done. That rules out:

- dimensions called `phony_dim_0`;
- a pixel size of `1.0` where the source stated a real one;
- a root group with no metadata;
- names that only mean something to whoever ran the ingest.

## Rules

- **Buy, do not build.** Parsing, virtualization and metadata come from
  maintained libraries: VirtualiZarr, tifffile, ngff-zarr, icechunk,
  imagecodecs. The local code turns a library's parsed output into chunk
  references. If you are writing a parser, a chunk-grid calculator or an
  OME-Zarr JSON document by hand, stop and find the library that does it.
  `docs/upstream.md` lists every local workaround and the upstream fix it waits on.
- **Fix a bug once, in one place.** A trap that bites twice is in the wrong
  layer. Push the fix down to the shared package where no caller can get it
  wrong, instead of repeating a workaround at each call site.
- **Match the source's chunking.** A chunk is the file's own storage unit: one
  tile, one strip, one frame, one compressed block. Never split or merge units
  to virtualize them. A unit with no stored bytes is a missing chunk, never a
  zero-length reference.
- **Never decode pixels to virtualize.** Decoding happens only to build a
  derived layer, such as an overview level, and then only once.
- **Verify, do not assert.** A claim about behaviour needs a run behind it.
  Read the store back and compare it against the source, decoded by the
  format's reference reader, before saying it works. Measure a number before
  quoting one.
- **Record every judgement call.** Choices the data does not determine (which
  directory is canonical, whether the file or the paper is right) go in the
  ingest's `LAYOUTS.md`, and in the report to whoever asked for the work.
- **Long runs report progress.** A job that runs for more than a minute prints
  a line at a steady interval: how far it has got, what is left, the current
  rate. Remote jobs forward those lines to the launching terminal.
- **Write down what the source taught you.** File-to-axis mappings go in the
  ingest's `LAYOUTS.md`. A lesson that applies beyond one archive goes in
  `docs/ingest.md` or `docs/formats.md`, stated as a general rule.
- **Plan, then report.** Before writing any data, write the ingest's `PLAN.md`
  for a human to approve; after the ingest, write its `REPORT.md`. The sections
  each needs are in `docs/ingest.md`. Both are public: no local paths, keys or
  credentials.

### Native ingests

An ingest copies the source's bytes only when reads in place would put a load on
the source that its owner did not provide for. A public bucket that serves
downloads, not range reads from every user, is such a case.
Our convenience is not a reason. The ingest's `PLAN.md` states the reason.

A native ingest relaxes two rules:

- **It decodes pixels once.** It does so only when no codec that every reader has
  can decode the source's chunks. It copies a chunk's bytes unchanged when one can.
- **It chooses the chunking for the reader.** The source's storage unit is the
  default. A different chunk shape needs a stated reason.

Every other rule holds, and three become stricter:

- **Verify each copied byte and each decoded tile against the source.** Compare
  copied chunks with the source bytes. Compare decoded tiles with the format's
  reference reader, on a sample that covers every level and layout.
- **Record the source objects.** The root attributes hold each object's URL, size,
  ETag and date, with the licence and the citation the publisher asks for.
- **Run beside the data.** The ingest runs in the source's region. A run from
  elsewhere measures nothing about the ingest.

## Layout

- `src/minus80/` is one package. `minus80.core` and `minus80.ome` are the
  shared layers; `minus80.parsers.<format>` holds one parser per format. None
  of it knows about any one archive. `docs/parsers.md` says what a parser
  takes and returns.
- Tests mirror the package under `tests/`.
- Each parser's third-party reader is an optional extra named after the
  parser. `minus80/__init__.py` and `minus80/parsers/__init__.py` import
  nothing, so a parser's dependencies load only when it is imported;
  `tests/test_imports.py` checks this.
- `ingests/<archive>/` holds one archive's ingest: scripts, `LAYOUTS.md`, a
  verify script and a notebook that opens the finished store cold. Each is a
  workspace member that depends on `minus-80`. Demos that combine stores live
  in a separate repository and only read them.
- An ingest imports from `minus80`, never from another ingest. Code only one
  archive needs stays in that ingest. Anything a second archive could use moves
  into `minus80`.
- OME-Zarr output is version 0.5 metadata; ingest an older source by upgrading
  it.
- Tabular data without a Zarr convention (per-target annotations, edge lists,
  proteome-wide tables) goes to an Iceberg table beside the store, not into
  Zarr arrays.

## Working in the workspace

- One uv workspace, one lockfile. Run everything through it: `uv run pytest`,
  or `uv run --package <ingest> python ingests/<archive>/ingest.py`.
  Dependency sources, including the VirtualiZarr pin, are set once in the root
  `pyproject.toml`. Never add a per-ingest source or a path dependency outside
  the repository.
- Install the hooks with `prek install` (or `pre-commit install`). They run
  gitleaks, strip notebook outputs, cap file size at 1 MB and run
  `scripts/check_leaks.py`.
- Never commit data, stores, caches, environments or notebook outputs. The
  `.gitignore` covers them; don't force-add past it.
- Notebooks are committed without outputs. Edit them in place; never regenerate
  a notebook from a builder script.
- Notes for your own working state (handoffs, plans, scratch measurements) go
  in `.private/`, which is ignored. Nothing in the repository should only make
  sense to the person who wrote it.
- Comments explain why, never what. Write for the reader of the final file: no
  references to the edit, the old version or the conversation that produced it.
- Shared cloud resources are touched only inside your own prefix. Writing to a
  shared Arraylake organization, registering buckets or creating repos needs a
  maintainer's explicit approval for that action.
