# minus-80

Tooling and scripts for zero-copy ingest of biology data into [Icechunk](https://icechunk.io).

- `src/minus80/`: one Python package: shared layers and format parsers
- `ingests/`: one directory per archive, with its scripts, layout notes and demo notebook

```python
from minus80 import core, ome
from minus80.parsers import tiff
```

## src/minus80/

Generic pieces will be upstreamed when they are ready; domain-specific pieces
will stay here.

| | Headed upstream | Stays here |
| --- | --- | --- |
| `core` | repo setup, remote indexing, flat binary, pyramids | progress, decisions, benchmarks |
| `ome` | hybrid pyramids (ngff-zarr) | annotation and provenance conventions |
| `parsers` | locating chunks | mapping a format onto axes, units, coordinates |

**Shared layers**

| Module | What it does | Status |
| --- | --- | --- |
| `minus80.core` | Wrappers over VirtualiZarr: repo setup, remote-file indexing, flat-binary arrays, hybrid pyramids ([vs VirtualiZarr](src/minus80/core/VIRTUALIZARR.md)) | in repo |
| `minus80.ome` | OME-Zarr metadata over referenced arrays: images, labels, plates, collections | in repo |

**Parsers**: one format each, emitting VirtualiZarr chunk references. Each
parser's reader library is an extra: `uv sync --extra dicom`.

| Module | Format | Status |
| --- | --- | --- |
| [`minus80.parsers.tiff`](src/minus80/parsers/tiff/README.md) | TIFF, including whole-slide pyramids | in repo |
| `minus80.parsers.dicom` | DICOM CT, whole-slide and multiplex | moving |
| [`minus80.parsers.nd2`](src/minus80/parsers/nd2/README.md) | Nikon ND2 | in repo |
| `minus80.parsers.nwb` | NWB (HDF5) | moving |
| `minus80.parsers.flac` | FLAC audio | moving |
| [`minus80.parsers.zip`](src/minus80/parsers/zip/README.md) | Uncompressed zip members as chunks | in repo |

## ingests/

| Ingest | Archive | Status |
| --- | --- | --- |
| `camelyon` | CAMELYON16 and CAMELYON17 whole-slide pathology, with tumour masks and outlines | moving |
| `chammi` | CHAMMI-75 multi-channel microscopy from 75 studies | moving |
| `cellpainting` | Cell Painting Gallery plates as OME-Zarr HCS | moving |
| `idc` | NCI Imaging Data Commons: CT, segmentations, slides, multiplex imaging, Visible Human | moving |
| `idr` | Image Data Resource studies | moving |
| `ctc` | Cell Tracking Challenge training sets | moving |
| `dandi` | NWB recordings from the DANDI Archive, including Allen Brain Observatory 2-photon | moving |
| `sanctsound` | SanctSound hydrophone recordings with species detections | moving |
| `opencell` | OpenCell: 1,310 endogenously tagged human proteins, with localization and interaction tables | planned |

## Getting started

```bash
uv sync
uv run pytest
```

## Licence

Code: Apache 2.0. Each dataset keeps its own licence, recorded in its store.
