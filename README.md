# minus-80

Tooling and scripts for zero-copy ingest of biology data into [Icechunk](https://icechunk.io).

- `tools/`: format parsers and shared OME-Zarr and Icechunk helpers
- `ingests/`: one directory per archive, with its scripts, layout notes and demo notebook

## tools/

**Shared**

| Package | What it does |
| --- | --- |
| `virtual-core` | Object-store registry, Icechunk read and write helpers, progress reporting |
| `virtual-ome` | OME-Zarr metadata over referenced arrays: images, labels, plates, collections |

**Microscopy and pathology formats**

| Package | What it does |
| --- | --- |
| `virtual-tifffile` | TIFF pages, tiles and strips, from tifffile's parse |
| `virtual-dicom` | DICOM CT volumes, whole-slide and multiplex images |
| `virtual-czi` | Zeiss CZI |
| `virtual-nd2` | Nikon ND2 |
| `virtual-mrc` | MRC and MRCS cryo-EM and cryo-ET volumes |

**Other modalities**

| Package | What it does |
| --- | --- |
| `virtual-nwb` | Neurodata Without Borders (NWB) recordings, which are HDF5 |
| `virtual-oct` | Optical coherence tomography vendor files and raw spectra |
| `virtual-flac` | FLAC audio frames |

**Containers**

| Package | What it does |
| --- | --- |
| `virtual-zip` | Zip members, and the raw-Deflate codec that decodes them |

### Upstream or local

These tools are staging grounds. Anything that isn't specific to biology, such
as a parser fix, a codec or a write path, is meant to move into the library
that owns it: VirtualiZarr, virtual-tiff, imagecodecs, async-tiff or
obspec-utils. `UPSTREAM.md` tracks each item and its status, and the local
copy is deleted once the fix is released.

What stays here is domain knowledge: how a format's own structure (NWB groups,
MRC headers, DICOM frames) maps onto named axes, units and coordinates, plus
the per-archive code in `ingests/`.

## ingests/

| Ingest | Archive |
| --- | --- |
| `ingest-camelyon` | CAMELYON16 and CAMELYON17 whole-slide pathology, with tumour masks and outlines |
| `ingest-chammi` | CHAMMI-75 multi-channel microscopy from 75 studies |
| `ingest-cellpainting` | Cell Painting Gallery plates as OME-Zarr HCS |
| `ingest-idc` | NCI Imaging Data Commons: CT, segmentations, slides and multiplex imaging |
| `ingest-idr` | Image Data Resource studies |
| `ingest-ctc` | Cell Tracking Challenge training sets |
| `ingest-dandi` | NWB recordings from the DANDI Archive, including the Allen Brain Observatory |
| `ingest-sanctsound` | SanctSound hydrophone recordings with species detections |

Packages are still moving in; a row with no directory yet hasn't landed.

## Getting started

```bash
uv sync
uv run pytest tools/virtual-core
```

## Licence

Code: Apache 2.0. Each dataset keeps its own licence, recorded in its store.
