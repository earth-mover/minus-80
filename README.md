# minus-80

Tooling and scripts for zero-copy ingest of biology data into [Icechunk](https://icechunk.io).

- `src/minus80/`: one Python package, with shared layers and format parsers
- `ingests/`: one directory per archive, with its scripts, layout notes and a notebook that opens the store
- [`docs/`](docs/README.md): how to ingest an archive, what each format allows, and upstream work

```python
from minus80 import core, ome
from minus80.parsers import tiff
```

## src/minus80/

**Shared layers**

| Module | What it does | Status |
| --- | --- | --- |
| [`minus80.core`](src/minus80/core/README.md) | Wrappers over VirtualiZarr: repo setup, remote-file indexing, flat-binary arrays, hybrid pyramids | in repo |
| [`minus80.ome`](src/minus80/ome/README.md) | OME-Zarr metadata over referenced arrays: images, labels, plates, collections | in repo |
| [`minus80.diagrams`](src/minus80/diagrams.py) | SVG building blocks and the shared style for each ingest's figures: panels, trees, links, array cubes, legends | in repo |

**Parsers**: one format each, emitting VirtualiZarr chunk references. Each
parser's reader library is an extra: `uv sync --extra dicom`.

| Module | Format | Status |
| --- | --- | --- |
| [`minus80.parsers.tiff`](src/minus80/parsers/tiff/README.md) | TIFF, including whole-slide pyramids | in repo |
| [`minus80.parsers.dicom`](src/minus80/parsers/dicom/README.md) | DICOM CT, whole-slide and multiplex | in repo |
| [`minus80.parsers.nd2`](src/minus80/parsers/nd2/README.md) | Nikon ND2 | in repo |
| [`minus80.parsers.nwb`](src/minus80/parsers/nwb/README.md) | NWB (HDF5) | in repo |
| [`minus80.parsers.flac`](src/minus80/parsers/flac/README.md) | FLAC audio | in repo |
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
| `sanctsound` | SanctSound hydrophone recordings with species detections | in repo |
| `opencell` | OpenCell: 1,310 endogenously tagged human proteins, with localization and interaction tables | planned |

## Getting started

```bash
uv sync
uv run pytest
```

## Licence

Code: Apache 2.0. Each dataset keeps its own licence, recorded in its store.
