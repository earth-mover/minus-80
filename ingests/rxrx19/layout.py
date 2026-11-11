"""The proposed store for RxRx19a and RxRx19b: its paths, and the attributes each node carries.

Both the ingest and ``diagrams.py`` build every node from these functions, so
the figures show the documents the ingest writes.

The tree
--------
The layout follows the Cell Painting Gallery JUMP ingest, one level per
directory of the source, with each dataset as a top-level group of one repo::

    <dataset>/<experiment>/Plate<n>/<row>/<column>/<site>/0
    rxrx19a  /HRCE-1      /Plate1  /AA   /2       /2     /0

- A plate is an OME-Zarr HCS plate. It declares every row (A–AF) and column
  (1–48) of its 1536-well format, and lists the wells that hold images.
- A well group lists its sites and carries ``metadata.csv``'s per-well fields.
- A site is an OME-Zarr image with one array, ``0``, of shape
  ``(channels, y, x)``. Each ``(1, y, x)`` chunk is one PNG, referenced in
  place in the zip and decoded by ``imagecodecs_png``.

Well names in the source, such as ``AA02``, are a row letter and a two-digit
column. The datasets' READMEs read ``AA02`` as "column AA, row 2"; the letters
run B–AE and the numbers 02–47, the inner rows and columns of a 1536-well
plate, so the letters are rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from minus80.core.jsonable import jsonable
from minus80.ome import image_annotation, image_attrs
from minus80.ome.image import channel_omero
from minus80.ome.plate import plate_attrs, plate_grid, well_attrs


@dataclass(frozen=True)
class Dataset:
    key: str
    folder: str
    title: str
    images: str
    metadata: str
    embeddings: str
    csv: str
    side: int
    citation: str
    doi: str
    pixel_size: float
    stains: tuple[str, ...] | None

    def https(self, path: str) -> str:
        return path.replace("gs://rxrx/", "https://storage.googleapis.com/rxrx/")


DATASETS = {
    "rxrx19a": Dataset(
        key="rxrx19a",
        folder="RxRx19a",
        title="RxRx19a: SARS-CoV-2 infection in human renal cortical epithelial and Vero cells",
        images="gs://rxrx/RxRx19a/RxRx19a-images.zip",
        metadata="gs://rxrx/RxRx19a/RxRx19a-metadata.zip",
        embeddings="gs://rxrx/RxRx19a/RxRx19a-DL-embeddings.zip",
        csv="RxRx19a/metadata.csv",
        side=1024,
        citation="Heiser et al. 2020",
        doi="10.1101/2020.04.21.054387",
        pixel_size=1.30,
        stains=(
            "Hoechst 33342 (nucleus)",
            "Concanavalin A (membrane glycoproteins)",
            "Phalloidin (actin)",
            "Syto14 (RNA)",
            "Wheat germ agglutinin (Golgi)",
        ),
    ),
    "rxrx19b": Dataset(
        key="rxrx19b",
        folder="rxrx19b",
        title="RxRx19b: a COVID-19 cytokine storm model in HUVEC",
        images="gs://rxrx/rxrx19b/rxrx19b-images.zip",
        metadata="gs://rxrx/rxrx19b/rxrx19b-metadata.zip",
        embeddings="gs://rxrx/rxrx19b/rxrx19b-dl-embeddings.zip",
        csv="rxrx19b/metadata.csv",
        side=2048,
        citation="Cuccarese et al. 2020",
        doi="10.1101/2020.08.02.233064",
        pixel_size=0.65,
        stains=None,
    ),
}

#: Where the pixel sizes and RxRx19a's stains are stated: each dataset's README in
#: recursionpharma/rxrx-datasets, at this commit. Both READMEs give "Physical resolution:
#: 0.65 micron/pixel". RxRx19b's 2048-pixel PNGs are the instrument's images, so 0.65 µm
#: is their pixel. RxRx19a's 1024-pixel PNGs are, per its dataset page, downsampled from
#: 2048-pixel originals, so its pixel is taken as 1.30 µm; its README does not say which
#: images the 0.65 describes, and RxRx1's README gives the same 0.65 for 512-pixel images.
#: RxRx19a's README maps w1–w5 to stains; RxRx19b's README maps none, so its channels are
#: named w1–w6.
README_COMMIT = "19883e707d"

#: The Arraylake repo that holds both datasets, each as a top-level group.
REPO = "arraylake://earthmover-biology/RxRx19"

#: Every row and column of a 1536-well plate.
ROWS, COLUMNS = plate_grid(1536)

#: The fields of ``metadata.csv`` that are the same for every site of a well.
WELL_FIELDS = ("well_id", "cell_type", "disease_condition", "treatment", "treatment_conc", "SMILES")

#: Each member's local header carries a 28-byte extra field (a Unix timestamp and
#: Unix ids), so its data starts 30 + name + 28 bytes after the header. The
#: directory alone shows this holds for every member: each member's span, from
#: its header to the next, is exactly 30 + name + 28 + its size.
LOCAL_EXTRA = 28


def split_well(well: str) -> tuple[str, str]:
    """``'AA02'`` → ``('AA', '2')``: the HCS row and column names."""
    return well[:-2], str(int(well[-2:]))


def plate_path(dataset: str, experiment: str, plate: int) -> str:
    return f"{dataset}/{experiment}/Plate{plate}"


def well_path(dataset: str, experiment: str, plate: int, well: str) -> str:
    row, column = split_well(well)
    return f"{plate_path(dataset, experiment, plate)}/{row}/{column}"


def site_path(dataset: str, experiment: str, plate: int, well: str, site: int) -> str:
    return f"{well_path(dataset, experiment, plate, well)}/{site}"


def root_document() -> dict:
    return {
        "title": "RxRx19: Recursion's COVID-19 Cell Painting screens",
        "source": "gs://rxrx/",
        "license": "CC-BY-4.0",
        "datasets": list(DATASETS),
    }


def dataset_document(dataset: Dataset) -> dict:
    return {
        "title": dataset.title,
        "citation": {"text": dataset.citation, "doi": dataset.doi},
        "license": "CC-BY-4.0",
        "source": {"images": dataset.images, "metadata": dataset.metadata, "embeddings": dataset.embeddings},
    }


def experiment_document(cell_type: str) -> dict:
    return {"cell_type": cell_type}


def plate_document(experiment: str, plate: int, wells: list[str], field_count: int) -> dict:
    return plate_attrs(
        name=f"{experiment} Plate{plate}",
        rows=ROWS,
        columns=COLUMNS,
        wells=sorted({split_well(w) for w in wells}),
        field_count=field_count,
    )


def well_document(sites: list[int], fields: dict) -> dict:
    """The well's sites and its ``metadata.csv`` fields; an empty CSV field becomes ``null``."""
    annotations = {f: None if fields[f] == "" else fields[f] for f in WELL_FIELDS}
    return {**well_attrs([str(s) for s in sites]), "annotations": jsonable(annotations)}


def site_document(dataset: Dataset, site_id: str, channels: int) -> dict:
    """One site as an OME-Zarr image, with the pixel size and stains from ``README_COMMIT``.

    A channel's label is its stain where the README names one, else its file
    suffix (``w1`` …).
    """
    labels = dataset.stains or tuple(f"w{c + 1}" for c in range(channels))
    ome = image_attrs(
        name=site_id,
        dims=("c", "y", "x"),
        level_shapes=[(channels, dataset.side, dataset.side)],
        scale={"c": 1.0, "y": dataset.pixel_size, "x": dataset.pixel_size},
        units={"y": "micrometer", "x": "micrometer"},
        omero=channel_omero([{"label": label} for label in labels], max_value=255),
    )
    return {"ome": ome, **image_annotation({"site_id": site_id}, source_url=dataset.images)}


def array_spec(dataset: Dataset, channels: int) -> dict:
    side = dataset.side
    return {
        "shape": (channels, side, side),
        "chunks": (1, side, side),
        "dtype": "uint8",
        "codec": "imagecodecs_png",
        "dimension_names": ("c", "y", "x"),
    }
