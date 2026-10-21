# minus-80

Large public bioimaging archives, opened as OME-Zarr without copying them.

Archives like the Cell Painting Gallery, CAMELYON and the Imaging Data Commons
publish millions of TIFF, DICOM, HDF5 and vendor files. Each file is readable
on its own, but the collection is not: to look at one well, one slide or one
protein you first work out the archive's file layout, then download whole files.

minus-80 builds an [Icechunk](https://icechunk.io) store for each archive. The
store holds an OME-Zarr hierarchy whose chunks are references to byte ranges in
the archive's own files, so the pixels stay where they are and nothing is
copied. A reader opens the store with any Zarr library and gets real axes,
physical units, resolution levels and the study's annotations beside the
pixels, and fetches only the chunks it asks for.

The name is the lab freezer: the archive's bytes are preserved where they are,
and come back out on demand.

## What's in this repository

The repository splits into two halves. `tools/` holds reusable packages that
know about file formats and OME-Zarr but nothing about any one archive.
`ingests/` holds one directory per archive: the scripts that map that archive's
files onto an OME-Zarr layout, a `LAYOUTS.md` recording each choice, a verify
script and a demo notebook. An ingest imports from tools, never from another
ingest.

### tools/

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

### ingests/

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

The packages are moving into this repository one at a time; a row above whose
directory does not exist yet has not landed.

## Getting started

The repository is one [uv](https://docs.astral.sh/uv/) workspace with a single
lockfile.

```bash
git clone https://github.com/earth-mover/minus-80
cd minus-80
uv sync
uv run pytest tools/virtual-core
```

Each ingest's README says how to open its published store, and its demo
notebook walks through what the store contains.

## Licence

The code is licensed under Apache 2.0. The datasets are not: each archive keeps
its own licence, and each store records that licence and the dataset's citation
in its root metadata.
