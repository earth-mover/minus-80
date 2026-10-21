# minus-80

Tooling and scripts for zero-copy ingest of biology data into [Icechunk](https://icechunk.io).

- `tools/`: format parsers and shared OME-Zarr and Icechunk helpers
- `ingests/`: one directory per archive, with its scripts, layout notes and demo notebook

## tools/

**Shared layers**

| Package | What it does |
| --- | --- |
| `virtual-core` | Repo creation, remote-file indexing, layouts VirtualiZarr lacks ([vs VirtualiZarr](tools/virtual-core/VIRTUALIZARR.md)) |
| `virtual-ome` | OME-Zarr metadata over referenced arrays: images, labels, plates, collections |

**Parsers**: one format each, emitting VirtualiZarr chunk references.

| Package | Format |
| --- | --- |
| `virtual-tifffile` | TIFF, including whole-slide pyramids |
| `virtual-dicom` | DICOM CT, whole-slide and multiplex |
| `virtual-czi` | Zeiss CZI |
| `virtual-nd2` | Nikon ND2 |
| `virtual-mrc` | MRC and MRCS (cryo-EM, cryo-ET) |
| `virtual-nwb` | NWB (HDF5) |
| `virtual-oct` | OCT vendor files and raw spectra |
| `virtual-flac` | FLAC audio |
| `virtual-zip` | Zip members, with a raw-Deflate codec |

Anything here that isn't biology-specific is headed upstream (VirtualiZarr,
virtual-tiff, imagecodecs, async-tiff, obspec-utils), tracked in `UPSTREAM.md`.
What stays is domain knowledge: how a format's structure maps onto axes, units
and coordinates.

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
