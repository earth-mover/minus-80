# minus-80

Tooling and scripts for zero-copy ingest of biology data into [Icechunk](https://icechunk.io).

- `tools/`: shared layers and format parsers, mostly staging for upstream libraries
- `ingests/`: one directory per archive, with its scripts, layout notes and demo notebook

## tools/

Nearly everything here is headed upstream, to VirtualiZarr, virtual-tiff,
imagecodecs, async-tiff or obspec-utils. What stays is domain knowledge: how a
format's structure maps onto axes, units and coordinates.

**Shared layers**

| Package | What it does | Status |
| --- | --- | --- |
| `virtual-core` | Wrappers over VirtualiZarr: repo setup, remote-file indexing, flat-binary arrays, hybrid pyramids ([vs VirtualiZarr](tools/virtual-core/VIRTUALIZARR.md)) | in repo |
| `virtual-ome` | OME-Zarr metadata over referenced arrays: images, labels, plates, collections | in repo |

**Parsers**: one format each, emitting VirtualiZarr chunk references.

| Package | Format | Status |
| --- | --- | --- |
| `virtual-tifffile` | TIFF, including whole-slide pyramids | moving |
| `virtual-dicom` | DICOM CT, whole-slide and multiplex | moving |
| `virtual-czi` | Zeiss CZI | moving |
| `virtual-nd2` | Nikon ND2 | moving |
| `virtual-mrc` | MRC and MRCS (cryo-EM, cryo-ET) | moving |
| `virtual-nwb` | NWB (HDF5) | moving |
| `virtual-oct` | OCT vendor files and raw spectra | moving |
| `virtual-flac` | FLAC audio | moving |
| `virtual-zip` | Zip members, with a raw-Deflate codec | moving |

## ingests/

| Ingest | Archive | Status |
| --- | --- | --- |
| `ingest-camelyon` | CAMELYON16 and CAMELYON17 whole-slide pathology, with tumour masks and outlines | moving |
| `ingest-chammi` | CHAMMI-75 multi-channel microscopy from 75 studies | moving |
| `ingest-cellpainting` | Cell Painting Gallery plates as OME-Zarr HCS | moving |
| `ingest-idc` | NCI Imaging Data Commons: CT, segmentations, slides, multiplex imaging, Visible Human | moving |
| `ingest-idr` | Image Data Resource studies | moving |
| `ingest-ctc` | Cell Tracking Challenge training sets | moving |
| `ingest-dandi` | NWB recordings from the DANDI Archive, including Allen Brain Observatory 2-photon | moving |
| `ingest-sanctsound` | SanctSound hydrophone recordings with species detections | moving |
| `ingest-opencell` | OpenCell: 1,310 endogenously tagged human proteins, with localization and interaction tables | planned |

## Getting started

```bash
uv sync
uv run pytest tools/virtual-core
```

## Licence

Code: Apache 2.0. Each dataset keeps its own licence, recorded in its store.
