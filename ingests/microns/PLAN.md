# MICrONS minnie: ingest plan

## Summary

The plan brings the whole MICrONS "minnie" release into one virtual Icechunk
store: electron microscopy, segmentation, two-photon calcium imaging, behavior,
stimuli and the tables that link them. All of it was recorded from one mouse.
Every array references the archive's own bytes in place, in BossDB's bucket
`s3://bossdb-open-data`, DANDI's bucket `s3://dandiarchive` and the
consortium's Google Cloud buckets. Nothing is decoded to build the store.

MICrONS is the largest public dataset that combines functional imaging and
connectomics in one animal. Its parts are published in four places (BossDB,
DANDI, Google Cloud and a database dump), in five formats, with metadata in a
sixth. A store a stranger can open, with the EM, every segmentation release and
every two-photon field in one place and placed in space, is the point of the
ingest.

- **EM and segmentation.** Each Neuroglancer precomputed volume becomes one
  OME-Zarr 0.6 multiscale image, with one Zarr chunk per precomputed chunk.
  The EM and every segmentation of a volume sit in one 0.6 scene, placed by
  translation.
- **Two-photon scans.** Each imaging field of each of the 19 scans becomes one
  `(t, y, x)` image, one chunk per frame, referencing the uncompressed BossDB
  TIFFs. Pixel size, depth, field position and frame times come from the DANDI
  NWB files.
- **Processed functional data.** Traces, ROI masks, behavior and the stimulus
  shown come from the DANDI NWB files, referenced in place.
- **Tables.** The synapse graphs, nucleus and cell-type tables, the
  functional-to-EM match and proofreading status go to Iceberg tables beside
  the store. Meshes, skeletons and other files with no Zarr form are listed by
  URL.
- **Releases as versions.** The proofread segmentation exists in four releases
  (v117 on BossDB; v343, v943 and v1300 on Google Cloud). Each is committed in
  order and tagged, so a reader can check out the release a paper used.

**Asked of the reviewer:** approve the layout and the trial in step 1, and
decide the judgement calls below. Nothing is written to a shared resource
before approval.

| # | Decision | Recommended |
|---|---|---|
| 1 | Scope | Everything in the release. The order is set by risk, not by importance (see "Steps") |
| 2 | Segmentation is its own image, not an OME-Zarr label image | Image; the spec requires a label image to have its parent's number of levels, and no segmentation here does |
| 3 | Which copy of each precomputed layer to reference, where a sharded and an unsharded copy both exist | The sharded copy, if the trial finds it complete |
| 4 | Which copy of the two-photon movies to reference | BossDB's TIFFs, v2 only |
| 5 | Metadata source for the scans: DANDI NWB or the DataJoint database dump | The NWB; the dump only for what the NWB lacks |
| 6 | Codecs for `jpeg` and `compressed_segmentation` chunks | Two thin Zarr codecs over maintained decoders (imagecodecs, `compressed-segmentation`), named in the root attributes |
| 7 | Meshes and skeletons | Listed by URL in tables beside the segmentation they belong to |
| 8 | The coregistration between two-photon and EM space | Stored as the source's transforms, in attributes; an affine in the scene only after checking it against the match table |
| 9 | Segmentation and table releases | One commit and tag per release, oldest first |
| 10 | Destination | A new Arraylake repo, `earthmover-biology/microns-minnie`, after the trial |

## The archive

{{ARCHIVE}}

## Proposed layout

### The tree

```
/                                          annotations: title, citation, DOIs, license, sources, codecs used,
                                           release history; ome: scene "minnie65" and "minnie35" (decision 8)
em/minnie65                                image (z, y, x) uint8, 9 levels, 8 × 8 × 40 nm at level 0
em/minnie35                                image (z, y, x) uint8, 9 levels
segmentation/minnie65/seg                  image (z, y, x) uint64, proofread segmentation, current release
segmentation/minnie65/autoseg              image, automated segmentation (v0)
segmentation/minnie65/ws                   image, watershed supervoxels
segmentation/minnie65/clefts               image uint32, synaptic clefts
segmentation/minnie65/nuclei               image uint32, nuclei
segmentation/minnie35/{seg,autoseg,ws,clefts}
microct/17797_1R                           image (z, y, x), 0.94 µm micro-CT of the same brain
two-photon/structural/<stack>              image (z, y, x), the vasculature stacks the fields register into
two-photon/scans/<session>-<scan>          ome: scene placing the scan's fields; annotations: rate, depths, nframes
two-photon/scans/<session>-<scan>/field-<n>          image (t, y, x) int16, one chunk per frame
two-photon/scans/<session>-<scan>/processed          the NWB's processing/ophys, behavior and stimulus, referenced
tables (Iceberg, beside the store)         synapses_minnie65, synapses_minnie35, nucleus_detection,
                                           nucleus_neuron_classification, cell_types, proofreading_status,
                                           functional_coregistration, digital_twin_units, files (meshes,
                                           skeletons, models, database dump, documentation)
```

Scan 4-7, field 1, is at `two-photon/scans/4-7/field-1`: an array of shape
(40000, 440, 248) int16 with chunks (1, 440, 248), whose 40,000 chunks
reference every eighth frame of `functional_scan_17797_4_7_v2.tif`. The EM's
level 0 is at `em/minnie65/0`: an array of shape (13088, 180224, 212992) with
chunks (16, 128, 128).

### Axes, units, levels

- **EM and segmentation** are `(z, y, x)` in nanometers, from each layer's
  `info`: `resolution` gives the scale per level and `voxel_offset` the
  translation. Precomputed stores a chunk's voxels with x varying fastest,
  which is C order for `(z, y, x)`, so no transpose codec is needed. Each level
  is one precomputed scale, in the source's order. The layers' levels differ
  (the EM has 9, the proofread segmentation 11, nuclei 5), which is why
  segmentation cannot be a label image of the EM (decision 2).
- **Two-photon fields** are `(t, y, x)`. The TIFF stores a frame as 440 rows of
  248 pixels; the documentation says rows run lateral to medial and columns
  posterior to anterior, and the NWB stores the same field as (…, 248, 440).
  Which copy is transposed is checked in the trial before the axes are named.
  `y` and `x` are 2.5 µm for 18 scans and 1.67 µm for scan 9-6. `t` is in
  seconds, with the scan's rate as the scale and the exact per-field frame
  times kept as an array beside the image.
- **Field placement.** Each scan is a 0.6 scene whose coordinate system is the
  microscope's motor coordinates in micrometers; each field is placed by its
  center from the NWB's `origin_coords` (equal to the database's `Field` table)
  and its depth.
- **Units** are `nanometer` for EM space and `micrometer` and `second` for
  two-photon space.

### Referenced, written, beside

| product | how |
|---|---|
| EM, segmentation, micro-CT (precomputed) | referenced: one chunk per precomputed chunk, byte ranges inside shards or whole unsharded objects |
| two-photon movies (BossDB TIFF v2) | referenced: one chunk per frame, uncompressed |
| structural stacks (BossDB TIFF) | referenced |
| traces, masks, behavior, stimulus at scan rate (DANDI NWB) | referenced: the NWB's HDF5 chunks |
| OME documents, scenes, per-scan and per-field metadata | written: JSON attributes |
| synapse graphs, nucleus and cell-type tables, match table, proofreading status, digital-twin unit properties | Iceberg tables beside the store |
| meshes, skeletons, model weights, the DataJoint dump and its containers, PDFs | Iceberg table of files: URL, size, what it belongs to |
| stimulus movies (AVI) | to decide after checking their codec; the NWB holds the stimulus at scan rate in any case |

### Layouts considered and rejected

- **Segmentation as label images under the EM.** It is how a viewer pairs
  segmentation with its image. But "the JSON array associated with the
  `datasets` key MUST have the same number of entries (scale levels)" (0.6,
  `labels` metadata), and no MICrONS segmentation has the EM's 9 levels at the
  EM's resolutions. Resampling to fit would mean decoding and writing
  hundreds of terabytes.
- **One array per scan, `(t, field, y, x)`, matching the TIFF.** It mirrors the
  file. But the fields of a scan are different places at different depths,
  and an image may have "2 or 3 entries of `type:space`" and one other axis;
  a field axis would be a custom axis holding positions. One image per field
  in a scene says where each field is.
- **Referencing the NWB's copy of the movies.** It would keep each scan in one
  file. Its chunks are (2254, 14, 14) gzip, so showing one frame reads 576
  chunks, each 2,254 frames long. The TIFF frame is one uncompressed read.
- **Merging minnie65 and minnie35 into one EM array.** They are aligned in one
  coordinate frame, but they are two precomputed volumes with their own chunk
  grids. Merging them would split or pad chunks, which the source's chunking
  rule forbids. A scene places both.

## What has been tested

All checks below ran on 2026-11-12, from a laptop in California unless stated.

- **Survey.** {{SURVEY}}
- **A two-photon TIFF** (`functional_scan_17797_4_7_v2.tif`): an uncompressed
  BigTIFF written by tifffile, 320,000 pages of 440 × 248 int16, one strip per
  page, the strips back to back from byte 406. The scan has 40,000 volumes of
  8 fields, field-major within each volume, as the documentation says. Read
  from us-east-2, the first 16 frames hold values from −67 to 32,422, 0.2% of
  them negative, so the data are int16 as the file says and not uint16 as the
  documentation says. Each field's mean differs (1,528 to 3,484), as eight
  interleaved fields would.
- **A DANDI NWB** (`sub-17797_ses-4-scan-7_behavior+image+ophys.nwb`, 71 GB),
  read from a VM in us-east-2 in 27 s; the same read from the laptop did not
  finish in 10 minutes:
  - one `TwoPhotonSeries` per field, (40000, 248, 440) int16, gzip level 4,
    chunks (2254, 14, 14), with per-field `timestamps`;
  - one `ImagingPlane` per field: `grid_spacing` 2.5 µm, `origin_coords`,
    indicator GCaMP6, excitation 920 nm, areas VISp, VISrl, VISlm and VISal,
    depths 80, 220, 360 and 500 µm; `imaging_rate` is NaN, and the rate (6.3009
    Hz) is only in each series' description;
  - per field, fluorescence and deconvolved traces, `PlaneSegmentation` masks
    with EM identifiers (`pt_root_id`, `pt_supervoxel_id`, `pt_x/y/z_position`),
    and summary images; eye, pupil and treadmill series; the stimulus movie,
    (381054, 144, 256, 3) uint8, one frame per chunk; trial tables for the
    three stimulus types.
- **The EM's precomputed `info`** for minnie65: 9 scales, 8 × 8 × 40 nm to
  2048 × 2048 × 1280 nm, all sharded (`neuroglancer_uint64_sharded_v1`). The
  8 nm scale is `raw` with gzip inside the shard and chunks of 128 × 128 × 16;
  the eight coarser scales are `jpeg` with 64³ chunks.

Nothing has been parsed into a store yet.

## Judgement calls

1. **Scope.**
   - *Evidence:* the release is one animal, and its value is the links between
     its parts: a two-photon ROI names its EM cell, a synapse names two
     segments, a segment has a mesh.
   - *Recommended:* everything, in the order of the steps below. The first
     store holds the minnie65 EM and the two-photon data; each later step is
     its own commit.
2. **Segmentation as images.**
   - *Evidence:* the label rule above. Each segmentation is a different volume
     with its own extent, offset and levels.
   - *Recommended:* one image per segmentation layer, placed in the scene beside
     the EM, with `annotations` naming the release and the source. A viewer
     that pairs a label image with its image will not pair these
     automatically.
3. **Sharded or unsharded copies.**
   - *Evidence:* {{COPIES}}
   - *Recommended:* the sharded copy where it is complete, because one shard
     index lists thousands of chunks and an unsharded scale is millions of
     objects to list. Record the other copy's URL.
4. **The two-photon movies.**
   - *Evidence:* the BossDB TIFF is uncompressed with one strip per frame; the
     NWB copy's chunks span 2,254 frames. v2 of each TIFF (January 2023)
     "corrected pixel over/under-flow error in export"; v1 is still in the
     bucket.
   - *Recommended:* reference the v2 TIFFs, and record v1's URLs.
   - *Cost:* the store references two files per scan, the TIFF and the NWB.
5. **Scan metadata.**
   - *Evidence:* the NWB holds the field geometry, depths, timestamps, areas
     and the ROI-to-EM links. The DataJoint dump is 116 GB of MySQL and holds
     more (raster and motion correction, registration into the structural
     stack, the coregistration transforms).
   - *Recommended:* the NWB first. Read the dump once, on a VM, for the tables
     the NWB lacks: `Registration`, `Coregistration`, `RasterCorrection`,
     `MotionCorrection`.
6. **Codecs.**
   - *Evidence:* three chunk encodings occur. `raw` with gzip decodes with
     Zarr's gzip codec. `jpeg` chunks are one 2-D JPEG of the chunk flattened,
     which imagecodecs decodes but does not reshape. `compressed_segmentation`
     has a maintained decoder from the Seung lab. Edge chunks are stored
     clipped to the volume where a scale's size is not a whole number of
     chunks, so a codec must pad.
   - *Recommended:* two Zarr codecs in `minus80`, each a few lines over its
     library, registered under a name the root attributes record. The trial
     checks edge chunks against CloudVolume's reads.
7. **Meshes and skeletons.**
   - *Evidence:* precomputed meshes are a multi-resolution Draco format and
     skeletons are SWC files; neither has a Zarr or OME-Zarr convention.
   - *Recommended:* an Iceberg table of files per segmentation: segment ID,
     URL, size, format. A viewer pointed at the original layer still finds
     them.
8. **Coregistration.**
   - *Evidence:* the Allen Institute's transforms between two-photon and EM
     space are linear or spline, both directions, in the database's
     `Coregistration` table. OME-Zarr 0.6 scenes can hold an affine; readers
     "SHOULD parse" it. A spline has no 0.6 form short of a displacement
     field.
   - *Recommended:* store the transforms as the source gives them, in the root
     attributes. Add the linear transform to the scene only if it places the
     match table's units on their EM cells.
9. **Releases.**
   - *Evidence:* v117 of the proofread segmentation is on BossDB; v343, v943
     and v1300 are only in `gs://iarpa_microns`. CAVE's tables are archived
     per release in `gs://mat_dbs/public/minnie65_phase3_v1/`. Arraylake
     already references Google Cloud chunks (the SanctSound store).
   - *Recommended:* commit v117 first, then each later release as a commit
     that replaces `segmentation/minnie65/seg`, tagged `v343`, `v943`,
     `v1300`; the tables follow the same tags.
10. **Destination.**
    - *Recommended:* `earthmover-biology/microns-minnie` on Arraylake, created
      after the trial, with virtual chunk containers for
      `s3://bossdb-open-data/iarpa_microns/`, `s3://dandiarchive/` and
      `gs://iarpa_microns/`.

## Steps, scope and risk

Every step runs on Coiled, beside the data: BossDB is in us-east-1, DANDI in
us-east-2. Nothing runs from a laptop but tests on fixtures.

1. **Parsers and codecs.** Add `minus80.parsers.precomputed` (sharded and
   unsharded layers; the sharding hash and index decoding from CloudVolume's
   `ShardingSpecification` rather than written here) and the two codecs. Test
   them on small fixtures served by the local range server.
2. **Trial, on a branch of a local repo on a VM.** A few hundred shards of the
   minnie65 EM at every scale, one segmentation layer, one scan (TIFF and
   NWB). It measures:
   - references per second when indexing shards, and the real chunk count per
     shard;
   - commit time and manifest size per million references in Icechunk 2;
   - edge chunks and pixels equal to CloudVolume's reads;
   - the two-photon orientation (decision in "Axes").
3. **Count every chunk.** Read every shard index of every sharded layer, and
   list every unsharded scale. This gives the store's true reference count,
   which the trial's rate turns into a time and a manifest size. Stop here if
   the count does not fit one repo, and report.
4. **Phase 1 into Arraylake:** the minnie65 EM, all 19 scans, the structural
   stacks and the tables. One commit per part.
5. **Phase 2:** minnie65 segmentation (v117), then minnie35, then micro-CT.
6. **Phase 3:** releases v343, v943 and v1300, and the CAVE tables per release.
7. **Verify** each phase with a short store check (`docs/ingest.md`): every
   source listed is present, the documents validate, and a few chunks per
   layer equal CloudVolume's or tifffile's decode.

**Not a goal:**
- re-segmenting, re-meshing or recomputing anything the consortium computed;
- CAVE's live, dynamic segmentation (`graphene://`); the store holds the
  static releases;
- the training ground truth on Zenodo.

**Writes that need approval:** the trial repo (own prefix in the sandbox
account), the Arraylake repo and every phase written to it, the Iceberg tables,
and any Coiled run expected to cost more than $10.

**Risks.**

- **Reference count.** Up to about 1.9 billion chunks per full-resolution layer
  if every chunk exists. Icechunk 2 has not been measured at this size. Step 3
  gives the real count before anything is committed.
- **Snapshot size.** A reader fetches the snapshot on open. The store has
  hundreds of arrays, not millions, so this is not expected to bind; the
  manifests are what grow.
- **Codecs outside the Zarr core.** A reader needs `minus80`'s codecs
  installed to decode `jpeg` and `compressed_segmentation` chunks. The root
  attributes name them.
- **Clipped edge chunks** could decode to the wrong shape if a codec pads
  wrongly; the trial compares edge chunks with CloudVolume.
