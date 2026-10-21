# minus-80

Public bioimaging archives as OME-Zarr in [Icechunk](https://icechunk.io), without copying the data.

- `tools/`: format parsers and shared OME-Zarr and Icechunk helpers
- `ingests/`: one directory per archive, with its scripts, layout notes and demo notebook

## tools/

| Package | What it does |
| --- | --- |
| `virtual-core` | Object-store registry, Icechunk read and write helpers, progress reporting |
| `virtual-ome` | OME-Zarr metadata over referenced arrays: images, labels, plates, collections |
| `virtual-tifffile` | TIFF pages, tiles and strips as chunk references, from tifffile's parse |
| `virtual-zip` | Zip members as chunk references, and the raw-Deflate codec that decodes them |
| `virtual-dicom` | DICOM CT volumes, whole-slide and multiplex images |
| `virtual-nwb` | Neurodata Without Borders (NWB) files, which are HDF5 |
| `virtual-mrc` | MRC and MRCS cryo-EM and cryo-ET volumes |
| `virtual-czi` | Zeiss CZI microscopy |
| `virtual-nd2` | Nikon ND2 microscopy |
| `virtual-oct` | Optical coherence tomography vendor files and raw spectra |
| `virtual-flac` | FLAC audio frames as chunk references |

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
