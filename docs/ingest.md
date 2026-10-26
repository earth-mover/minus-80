# Ingesting an archive

This page covers how to shape, verify and run an ingest. What each file format
allows, and the library traps that come with it, are in
[formats.md](formats.md).

Every rule here serves one test: open the store with nothing else for
reference, and ask whether you can tell what it is. If the answer needs the
source directory, the original filenames or the person who ran the ingest, the
store has failed.

## Metadata

### Keep every piece of metadata the source carries

If the source states it, the store keeps it, however awkward it is to place.
Nothing raises an error when metadata is lost: when an NWB sampling `rate` attribute was
dropped, the time axis could not be rebuilt. Read the attributes back after
every write.

### Add what the source implies only when you are certain

If a value follows unambiguously from the file, write it: an RGB TIFF gets an
`omero` block (`minus80.ome.image.rgb_omero`), without which napari renders it
as noise. If a value would be a guess, leave it out and record the gap in an
attribute. Never fill in a format's default; TIFF's default of one pixel per
inch becomes 25,400 µm per pixel and passes every schema.

### Treat a stated value as a claim to check

A tag can be present and still wrong: some microscopes write a TIFF resolution
tag that is not the sample's pixel size. Cross-check pixel sizes against the
study's annotation table and the paper.

### Keep the spec's structure and the source's names

Never add a node the spec does not name; put extra metadata, such as the
source's OME-XML, in group attributes. Use real names in the source's order.
Never build an axis from sorted unique values, because sorting Cell Painting's
channel names puts AGP before DNA and nothing raises an error.

### Make the store state its own requirements

Everything needed to open the store goes in the store. Source prefixes go in
the repo config, where `minus80.core.icechunk_io.open_repo` reads them to
authorize virtual reads, and a codec outside the Zarr core is named in the root
attributes.

## Layout

### Read the spec's structures before designing a hierarchy

Before proposing a layout, read the OME-Zarr sections that could fit the
data: the axes rules, single images, `bioformats2raw.layout` collections and
HCS plates. Map the source onto the structure the spec defines for its kind of
data, and quote the rule behind each choice in `PLAN.md`. A well plate is an
HCS plate with one image per field of view, however many fields share a shape.
The JSON schema does not enforce every rule: an image may have only one
channel or custom axis, so stacking fields of view into `(fov, c, z, y, x)`
breaks a MUST and still validates.

### Survey the whole archive before building anything

Count every directory and file first; one study was reported finished with one
of its nine directories ingested. Probe one file per source with its own
header. When the same specimens appear in several formats, pick one canonical
representation and record the others in `LAYOUTS.md`.

### Decide stack or collection with evidence, in writing

A stack along an axis the experiment never had passes every check and is worse
than the directory it replaced. Read the study's metadata files, description
and paper before trusting filenames. Flag a study that does not form a cube
instead of forcing one, or padding it with singleton axes. Never infer what
channels mean from how many there are: three samples can be RGB or three
stains.

### Join annotations by exact key

When a key matches nothing, leave it blank and record the join coverage. A
blank is safer than a plausible mistake.

### Stay inside the spec's structural limits

OME-Zarr allows at most five axes, with channel before the spatial axes; a 6-D
cube is valid Zarr that no OME-Zarr tool can open. If the source stores `(y, x,
c)`, declare channel-first and add a transpose codec.

### Reproduce the source's errors and record them

A store that silently corrects its source is harder to trust than one that
reproduces a known error. Keep the source's value, write the finding in
`LAYOUTS.md` and recommend a fix upstream. Fields the store derives itself are
the exception.

### Count the arrays before publishing

Icechunk writes a manifest per array, so the array count sets the cost of a
commit: CHAMMI-75 commits in under a minute with its tables on one shared axis,
and did not finish in 25 minutes with them copied into each stack. Time a
commit against the real object store, because a local build hides the
per-object cost.

## Verification

### Treat passing checks as a floor

Each gate in `minus80.ome.verify` exists because a mistake got through once. No
gate can see a wrong shape, a partial ingest or a tenfold pixel size.

### Compare against the format's reference reader

Read the store back, compare it with the source decoded by the format's
reference reader, and report the counts. For lossy codecs, use the same decoder
on both sides: two conformant JPEG 2000 decoders can differ by one bit.

### Keep verification to a minute

Never read the whole archive to verify it. Run the gates that read only
metadata (schema, axes, pixel size, completeness against the source listing)
on every image. Check chunk references and pixels on a sample that covers
every distinct shape, codec and source layout: read each sampled chunk's
reference back from the store (VirtualiZarr's `IcechunkParser`), compare its
path, offset and length with the reference reader's index of the file, and
decode a plane against the reference reader. Every item went through the same
parse, so the sample stands for the rest. Checking every reference means
reading every file's index, which for OpenCell's 12,602 TIFFs took hours from
a laptop against the published repo.

### Open the store and look

A store can reference every byte and still render as noise. Open it in a
viewer, then again from a fresh shell, because a repo can depend on the build
shell's environment.

### Make every pixel traceable to its file

Every virtual array, or its group, names its source URL. The `provenance` gate
checks this.

### Check every item, not a sample

A rule that holds on the items you tried can fail on the rest. Cutting
CAMELYON's exclusion regions from its tumour outlines worked on the slides
tried, and erased 18 tumour islands on a slide that only the check over all 160
found.

### Raise on a missing value instead of defaulting

A lookup that falls back to a default turns a name mismatch into data. Compare
a table's value counts with the source before publishing.

### Verify the science, not only the bytes

A virtual store makes a wrong label as easy to share as a right one. Test each
guess about the data; Cell Painting's stage axis directions, for example, are
checked by edge continuity between neighbouring fields.

### Stop a run that overruns its expected time

Know the expected runtime before you start, and stop a run that is well past
it. A run that did not finish did not commit. Do not truncate a buffer to fix a
decode that comes up one element short, because that hides corruption.

## Running the ingest

### Run beside the data, and time remote reads

One ingest indexed 11 files per second from a laptop and 592 from the data's
region, and the laptop run dropped chunks. Price indexing before you start
([formats.md](formats.md#indexing-remote-files)). Local timings mislead,
because local reads are bound by memory bandwidth; time the read a user sees
against downloading the files.

### Decode only to build a derived layer, and only once

When an overview needs pixels, decode the bytes that indexing already
downloaded, once, and take everything else that needs pixels from that pass.

### Commit often, and merge parallel writers once

Commit in small steps, so that a late failure does not lose an hour of work.
When many workers commit to one branch, each rebases onto the others, which is
slow and fails. Fork one session per worker, merge the forks and commit once.
Commit shared parent groups before forking, because two forks that create the
same group cannot merge.

### Keep one repo per dataset, and try new sources on a branch

A reader fetches the whole snapshot on open, and Icechunk's snapshot cache
holds 500,000 nodes by default, about 45 Cell Painting plates. Ingest one file
from each new source to a branch and inspect it before writing to `main`.

### Launch cloud workers from a clean environment

Coiled copies your local environment, so a macOS wheel or an editable checkout
breaks Linux workers. It also reattaches to a live cluster of the same name,
old code included, so name each launch's cluster uniquely.

## Data beyond the images

### Find out what consumers use before designing the layout

The derived data is often the payload: JUMP holds 243 TB of numerical data
against 115 TB of images. Name each consumer and the layer they read.

### Put tables beside the store

Tabular data without a Zarr convention goes to an Iceberg table beside the
store. Keep identifiers as integers, since pandas turns an integer column into
floats when one row is missing.

### Store annotations in a format viewers read

No viewer reads vector annotations from inside Zarr, and OME-Zarr has no ROI
standard. CAMELYON's outlines are stored as ragged arrays and as GeoJSON that
QuPath imports, and are checked against the challenge's own masks.

### Take provenance text from the code that read the source

A description written as a constant is true for the first dataset and false for
the next. Check a new dataset's columns by eye on real rows.

## Writing up an ingest

Each ingest keeps two write-ups in `ingests/<archive>/`. Both are public: name
tools and services such as Coiled and Arraylake freely, and never include local
paths, keys or credentials. A PDF, when one is wanted, is a render of the
Markdown.

### `PLAN.md`, before any data is written

The agent writes the plan, and a human approves it before the ingest writes
anything.

1. **Summary**, with the decisions needed at the top.
2. **The archive**: size, files, formats, licence, and how people use it.
3. **Proposed layout**: the tree, axes, units and levels; what is referenced,
   what is written natively and what goes to an Iceberg table; layouts
   considered and rejected.
4. **What has been tested**, with measured numbers.
5. **Judgement calls**, each with a recommendation.
6. **Steps, scope and risk**, including every write that needs approval.

### `REPORT.md`, after the ingest

1. **What was built**, where it is, and how to open it.
2. **Verification** against the format's reference reader, with counts.
3. **Changes from the plan.**
4. **Judgement calls made.**
5. **Problems found in the source data.**
6. **Upstream issues hit**, linked to [upstream.md](upstream.md).
