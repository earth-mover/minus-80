# MICrONS minnie, phase 1: two-photon functional scans

## Summary

Phase 1 puts the 19 two-photon functional scans of the MICrONS mouse into
one virtual Icechunk store. The pixels stay where BossDB published them: each
chunk is one frame of BossDB's uncompressed TIFFs, read in place. DANDI's NWB
files of the same scans supply what the TIFFs lack: each field's pixel size,
depth and position, the frame rate and the time of every volume.

BossDB exported the scans twice, and the store keeps both as two commits on
`main`. The first commit references the 2021 export (v1) as it declares its
pixels, uint16, and is tagged `v1`. The second references the 2023 export
(v2), which declares the same bytes int16, and is tagged `v2`. The trial
found that the two exports differ only in that declaration.

**Asked of the reviewer:** approve the layout and the run below, and decide
the judgement calls. Nothing has been written outside a local trial repo.
The destination repo, `earthmover-biology/MICrONS` (private), exists with one
virtual chunk container for the BossDB scan prefix; the run waits on write
access to it.

| # | Decision | Recommended |
|---|---|---|
| 1 | Give each field a size-1 z axis, so the scene can place it at its depth | Yes; the alternative is (t, y, x) with depth only in attributes |
| 2 | Place the fields with a 0.6 scene per scan, not a translation inside each image | Scene |
| 3 | Keep the volume times once per scan, not once per field | Once per scan; the NWB gives every field the same times |
| 4 | Keep v1 as the first commit, with its wrong dtype | Yes, as planned; tagged so that an analysis made on v1 can be checked |
| 5 | Scans 5-4, 5-5 and 8-4, published only in `platinum_camovies/` and absent from DANDI | Leave out of phase 1: they have no NWB metadata |
| 6 | Destination | Decided: `arraylake://earthmover-biology/MICrONS` (private), container `s3://bossdb-open-data/iarpa_microns/minnie/functional_data/two_photon_functional_scans/`, anonymous, us-east-1 |

The figures are in [`diagrams/`](diagrams/): the archive and what phase 1
takes from it ([`source.svg`](diagrams/source.svg)), how one scan's TIFF and
NWB file become its groups ([`mapping.svg`](diagrams/mapping.svg)), and the
store ([`store.svg`](diagrams/store.svg)). [LAYOUTS.md](LAYOUTS.md) records
the evidence behind each mapping.

## The archive

MICrONS imaged the visual cortex of one mouse (17797), first with a
two-photon mesoscope while it watched movies, then with electron microscopy.
Phase 1 covers the functional imaging:

- **BossDB TIFFs** (CC BY 4.0), in
  `s3://bossdb-open-data/iarpa_microns/minnie/functional_data/two_photon_functional_scans/`,
  us-east-1, anonymous reads. One TIFF per scan per export, 38 files, 1.34 TB
  per export. Each file is an uncompressed movie of `volumes × fields` frames,
  the fields interleaved within each volume. The directory's technical
  documentation (v2, July 2023) gives the scan groups: 15 scans of 40,000
  volumes × 8 fields at ~6.3 Hz, scan 4-9 stopped at 35,112 volumes, scans 9-3
  and 9-4 with 50,000 × 6 at ~8.6 Hz, and scan 9-6 with 57,000 × 4 at ~9.6 Hz
  and larger frames. That is 144 fields and 5,908,896 frames per export.
- **`functional_data/platinum_camovies/`** holds 22 TIFFs named
  `CaMovie_17797-<session>-<scan>-v1.tif`. Nineteen have the same ETag as the
  v1 file of the same scan, so they are copies of it, and the store leaves
  them out. The other three, scans 5-4, 5-5 and 8-4, appear nowhere else in
  the bucket (see decision 5).
- **DANDI 000402** (CC BY 4.0, version 0.230307.2132), in `s3://dandiarchive`,
  us-east-2. One NWB file per scan, 62 to 96 GB, holding the same movies again
  as gzip-compressed HDF5, the field metadata, and the processed data.

People use the scans to relate a neuron's visual responses to its
connections in the EM volume. Most analyses start from the processed traces
(phase 2); the movies are what those traces were extracted from, and what a
reader re-segments or checks them against.

## Proposed layout

### The tree

```
/                                     title, description, license, citations, sources, layout, tiff_export
two-photon                            description; one entry per scan: fields, volumes, rate
two-photon/<session>-<scan>           ome.scene: coordinate system "motor" (t, z, y, x), one translation per field
                                      scan: animal, session, scan, counts, rate, depths, NWB and TIFF URLs
two-photon/<session>-<scan>/volume_times      (volumes,) float64 seconds, native
two-photon/<session>-<scan>/field-<n>         image, (t, z, y, x)
two-photon/<session>-<scan>/field-<n>/0       (volumes, 1, rows, columns), one chunk per TIFF page
```

Scan 4-7, field 3 is at `two-photon/4-7/field-3`. Its array `0` has shape
(40000, 1, 440, 248) and chunks (1, 1, 440, 248), and chunk (v, 0, 0, 0)
references page `v × 8 + 2` of `functional_scan_17797_4_7_v2.tif` (at tag
`v2`).

### Axes, units, levels

- **Axes (t, z, y, x).** OME-Zarr orders time, then space. `y` runs along the
  TIFF's rows (lateral to medial), `x` along its columns (posterior to
  anterior); the trial confirmed `x` against the overlap of coplanar fields.
- **Scale.** `t` is 1 / rate seconds, from the rate in the NWB series
  description; `y` and `x` are the NWB `grid_spacing`, 2.5 µm (1.667 µm for
  scan 9-6); `z` is a placeholder of 1 µm, named as one in the attributes,
  because a field is one optical section and nothing states its thickness.
- **Declaring the images.** The OME-Zarr metadata starts at the scans. Each
  scan group is a 0.6 scene, which names its field images by path; the root
  and `two-photon` are plain groups with descriptions, because the spec has
  no structure for a group of scenes. LAYOUTS.md quotes the spec sections.
- **Placement.** Each scan is an OME-Zarr 0.6 scene, "for images that share a
  spatial relationship". Its coordinate system, `motor`, is the microscope's
  stage coordinates. The scene translates each field by `(t, z, y, x)`: the
  scan's first volume time, the field's depth, and the center of its first
  pixel, computed from the field center the NWB states.
- **Time.** The scale is nominal: over a whole scan the volume times run 2.5
  to 3.0 s longer than the stated rate implies. `volume_times` is exact.
- **Levels.** One. The TIFF has no pyramid, and a frame is 440 × 248 pixels,
  small enough to read whole.

### Referenced, written, beside

| Product | How |
|---|---|
| Frames (BossDB TIFF, both exports) | referenced: one chunk per page, uncompressed, Zarr's `bytes` codec only |
| Volume times (NWB timestamps) | written natively, Blosc zstd with byte shuffle, 280 to 456 KB per scan before compression |
| Field metadata (NWB imaging planes, series attributes) | attributes on each field image, as the file states them |
| NWB movies | not referenced; the TIFF frames are the same pixels and cost one request each |

Every array a reader opens is in Zarr's v3 core: any Zarr reader in any
language decodes it.

### Layouts considered and rejected

- **(t, y, x) with depth in attributes.** Valid and simpler, but no OME-Zarr
  reader can then place a field in depth, and the scene could only place it
  in y and x. See decision 1.
- **A translation inside each image, no scene.** A viewer that reads only
  single images would place each field; but each image would carry its
  scan's coordinate system implicitly, and the spec defines the scene for
  exactly this case. The zebrafish ingest places its embryos the same way.
  See decision 2.
- **A `bioformats2raw.layout` collection at the root.** An `OME` group
  listing all 144 field paths would let collection readers find the fields,
  but it is a second, flat list beside the hierarchy that holds them, and the
  spec marks the layout transitional, for filesets that already use it.
  Each scene declares its own images.
- **One array per scan, (t, field, y, x).** The fields of a scan sit at
  different places and depths, and one array has one transform, so it cannot
  place them. OME-Zarr also allows an image only one axis that is neither
  space nor time.
- **A `frame_times` array in each field image.** The 8 arrays of a scan would
  be identical, and an image group holding an extra array is a node the spec
  does not name. See decision 3.
- **Referencing the NWB movies.** They repeat the TIFF pixels exactly,
  transposed (see "Field mapping" below), so they add no data. Their gzip
  chunks are (2254, 14, 14): one frame touches 576 chunks of 2,254 frames
  each. A TIFF frame is one uncompressed strip, one 218 KB read.
- **Only v2.** Simpler, one commit. But v1 was the public export for 18
  months, and an analysis made on it read different values; the first commit
  lets that be checked. See decision 4.

## What has been tested

All on a laptop, reading the buckets over the internet, on 2026-11-19.

**Field mapping.** The NWB movies repeat the TIFF pixels exactly,
transposed, which proves the mapping. In scan 4-7 at volume 1000, a 28 × 42
patch of each NWB `TwoPhotonSeries<k>` equals TIFF page `1000 × 8 + (k − 1)`,
transposed, exactly, and matches no other field's page. Field `n` of the store is NWB
series `n`. The NWB conversion code fills `origin_coords` from the DataJoint
`Field` table's field center, so the store treats it as the center.

**Layout of every TIFF.** The TIFF parser read every directory of all 38
files. Every file's pages are contiguous and of one size, and their offsets
come from the files' own directories. The first directory sits at byte 16,
before the pixels; the rest are packed after them, 236 bytes each in v2 and
296 in v1, 51 to 90 MB per file and 3.1 GB in all.

**v1 against v2.** The two exports hold the same pixel bytes. 72 pages
compared byte for byte, in scans 4-7, 9-3 and 9-6, are identical, so the
second commit reinterprets the same values: the dtype changes from uint16 to
int16, and v1 values above 32,767 read as negative in v2 (0.1 to 0.3% of
pixels, from −167 to −1). The second commit still references the v2 files,
because they are the objects BossDB publishes as current. The
files differ in `SampleFormat` (UINT in v1, INT in v2), in three resolution
tags that v1 carries (1/1, unit NONE), and so in their offsets. No v2 value
was near the int16 maximum. The ingest repeats this check on the first,
middle and last page of every scan before writing the second commit, and its
message says so.

**Timing.**

| Step | Laptop | Notes |
|---|---|---|
| Probe one file's layout | 0.5 to 0.7 s | a HEAD and two range reads |
| Fetch one file's directory block | 2.4 s for 75.5 MB in one GET; 5 to 9 s in 32 MB blocks under load | |
| Parse 320,000 directories with tifffile | 8.6 s alone; 17 to 34 s with fetch, 3 files at once, on a loaded machine | peak memory 1.5 GB per parse |
| All 38 files | 364 s, 3 at a time | |
| Read one scan's NWB metadata | 1.2 s for one, 12 s for two with the listing | metadata only |

The library parse is fast enough, so the ingest uses tifffile and does not
read the directories itself.

**Test write.** Scans 4-7 (8 fields) and 9-6 (4 fields, 600 × 372) went into
a local repo, both commits and both tags, in 57 s, with 2.1 GB peak memory;
the repo is 13 MB. `verify.py` passed in 16 s, through both tags: the store
lists both scans and all 12 fields, the OME-Zarr documents validate, the
volume times equal the NWB timestamps, the scene places every field at the
center and depth the NWB states, and 8 patches of 14 × 14 pixels equal the
NWB copy of the same frames. The fixture test (`test_ingest.py`) checks every
frame of every field of a small synthetic scan, through both tags.

**Reading.** From the laptop, through the local repo: one frame in 0.12 s
once the repo is open, 100 consecutive frames in 1.45 s.

**Requests per file.** The parser makes a HEAD and two small range reads to
find the directories, then fetches the first 64 KB and the directory block,
the block in 32 MB pieces at once. The probe checks that the file's tail is
a uniform array of directories before the parser fetches all of it; without
the check, a file laid out otherwise could send the parser to fetch tens of
gigabytes of pixels.

## Judgement calls

1. **A size-1 z axis.** Each field is one optical section, at one of two to
   four depths per scan. With a z axis, the scene places it at its depth, and
   a viewer that reads scenes shows a scan as the stack of planes it was.
   The cost is an axis of length 1 with a scale nothing states (1 µm, named a
   placeholder) and an extra index in every read (`field[v, 0]`).
   **Recommended:** keep z.
2. **Scene, not image translations.** See "Layouts considered".
   **Recommended:** scene.
3. **Volume times once per scan.** Every `TwoPhotonSeries` in a file carries
   the same timestamps, which the conversion took from one per-scan table.
   They are the times of each volume, not of each field within it, and the
   store says so. **Recommended:** one `volume_times` per scan.
4. **Both exports.** The store reproduces v1's wrong dtype in the first
   commit and records the finding in LAYOUTS.md, as docs/ingest.md asks for a
   source's errors. `main` ends at v2. **Recommended:** both, as planned.
5. **Scans 5-4, 5-5 and 8-4.** `platinum_camovies/` holds three scans that
   are in neither the scan directory nor DANDI 000402. Each has the v1
   layout: 320,000 pages, the first directory at byte 16, directories of 14
   tags (296 bytes) packed after the pixels from byte 69,836,800,512. Without
   an NWB file there is no pixel size, field position, depth or volume time,
   and no v2 export, so they could only be stored as uint16 movies with
   placeholder geometry. **Recommended:** leave them out of phase 1, and ask
   the MICrONS team whether NWB metadata or a v2 export exists for them.
6. **Destination.** Decided: `earthmover-biology/MICrONS`, a private
   Arraylake repo. Its one virtual chunk container is `s3://bossdb-open-data/iarpa_microns/minnie/functional_data/two_photon_functional_scans/`,
   read anonymously in us-east-1, and the organization's access policy
   covers only that prefix. The ingest's source prefix is that exact string,
   so `arraylake_repository`'s container check passes. Later phases that
   reference other prefixes (DANDI's bucket, the EM) need their own
   containers and policy.

Two smaller choices are recorded in LAYOUTS.md rather than asked here: the
NWB `origin_coords` is used as the field center, as the conversion code and
the DataJoint schema state, against NWB's definition of the field; and the
NWB's inconsistent `field_of_view` and `dimension` orders are kept as stated.

## Steps, scope and risk

**Scope.** In: the 38 TIFFs in the scan directory and, from the 19 NWB files, the imaging-plane
metadata and timestamps. Out: EM, segmentation, micro-CT, structural stacks,
stimulus movies, the processed NWB data, the CAVE tables, `platinum_camovies/`
(copies of v1 plus three scans without metadata), and everything else in the
bucket.

**Steps.**

1. Approval of this plan and the judgement calls.
2. Write access to `earthmover-biology/MICrONS` for the ingest's token. The
   repo and its container exist.
3. Run 1, `runs/01_twophoton.sh`: one Coiled VM (`c6i.4xlarge`, us-east-1)
   with `coiled batch run`, writing both commits to `main`, then
   `verify.py --tag`, which tags them `v1` and `v2` only if every check
   passes. Icechunk refuses to reuse a deleted tag's name, so a tag made
   before the check could never be moved off a bad commit. Each commit's
   metadata names its export and scans, so a rerun after a failure writes
   only what is missing. *Needs approval: writes the store.*
4. Run 2, `runs/02_scenes.sh`: one metadata-only commit on main that
   brings run 1's store to the layout without the root collection. It
   deletes the `OME` group and replaces the root's and `two-photon`'s
   attributes with what the ingest now writes. It compares every scan group
   and field image with what the ingest would write and reports any
   difference, but never changes them; the dry run against the store found
   none. No array or chunk
   reference changes, and tags `v1` and `v2` stay on run 1's commits. It uses
   only Zarr's own operations on the Icechunk session: `del root["OME"]`
   deletes the group, and `group.attrs.put` replaces a group's attributes,
   which rewrites that group's `zarr.json` and nothing else. No node is
   renamed or moved, so the script needs no move. Icechunk's diff of the
   trial update lists two groups updated and one deleted, and no arrays or
   chunks. *Needs approval: writes the store.*
5. REPORT.md, with the run's snapshots, timings and verify output.

**Expected run.** About 2 minutes to install, under 1 minute for the NWB
metadata of 19 scans, and about 2 minutes per export: 19 scans, 8 at a time,
each fetching 51 to 90 MB of directories beside the bucket and parsing them
in 10 to 35 s. Under 10 minutes in all, with a hard limit of 30. The VM costs
under $1, reads in-region are free, and the cross-region NWB reads are a few
megabytes.

**Risks.**

- *Commit size.* Each export writes 5.9 million chunk references. Locally the
  two scans' 1.1 million references took 13 MB; the full store should be
  about 140 MB of manifests, and its commit time against Arraylake is
  unmeasured.
- *Reading a pixel over time.* One chunk per frame suits viewing frames and
  short clips. A pixel's whole trace reads every frame of the field: 8.7 GB
  for a 40,000-volume field. Traces belong to phase 2's processed data, and a
  rechunked copy is out of scope.
- *The NWB clock.* `volume_times` is in the NWB file's clock, which the
  conversion may have shifted to the earliest behavior timestamp. The store
  states the clock; phase 2's traces and behavior share it.

## Later phases

Phase 2, the processed data and the EM matches, is planned in
[PLAN-phase2.md](PLAN-phase2.md).

- The processed functional data in the NWB files: traces, ROI masks with
  their EM links, behavior, stimulus. The EM links need care. Each
  `PlaneSegmentation<n>` stores `pt_root_id`, `pt_supervoxel_id` and
  `cave_ids` as float64, which holds integers exactly only up to 2^53, so
  18-digit segment IDs come back rounded. The root IDs are also from the v343
  proofread segmentation (the conversion's `CAVEclient("minnie65_public_v343")`),
  long superseded. Join on the EM position (`pt_position`), which is exact,
  and look the IDs up again in the release in use.
- The two-photon structural stacks the fields register into, and the
  coregistration into EM space.
- EM and segmentation (Neuroglancer precomputed), with segmentation releases
  as commits.
- The CAVE and synapse tables, as Iceberg tables beside the store.
