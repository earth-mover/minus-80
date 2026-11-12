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
    readme: str


#: Recursion's public bucket. References record these gs:// URLs; reads go over HTTPS.
BUCKET = "gs://rxrx/"


def readable(url: str) -> str:
    """``url`` as this process reads it: a ``gs://rxrx/`` URL over HTTPS, anything else as given."""
    return url.replace(BUCKET, "https://storage.googleapis.com/rxrx/")


#: Where the pixel sizes and RxRx19a's stains are stated: each dataset's README in
#: recursionpharma/rxrx-datasets, at this commit. Both READMEs give "Physical resolution:
#: 0.65 micron/pixel". RxRx19b's 2048-pixel PNGs are the instrument's images, so 0.65 µm
#: is their pixel. RxRx19a's 1024-pixel PNGs are, per its dataset page, downsampled from
#: 2048-pixel originals, so its pixel is taken as 1.30 µm; its README does not say which
#: images the 0.65 describes, and RxRx1's README gives the same 0.65 for 512-pixel images.
#: RxRx19a's README maps w1–w5 to stains; RxRx19b's README maps none, so its channels are
#: named w1–w6.
README_COMMIT = "19883e707da71da330b97e0a912e20290f87c67e"
README_URL = "https://github.com/recursionpharma/rxrx-datasets/blob/{commit}/{folder}/README.md"
README_RAW = "https://raw.githubusercontent.com/recursionpharma/rxrx-datasets/{commit}/{folder}/README.md"

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
        readme=README_RAW.format(commit=README_COMMIT, folder="rxrx19a"),
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
        readme=README_RAW.format(commit=README_COMMIT, folder="rxrx19b"),
    ),
}

#: The Arraylake repo that holds both datasets, each as a top-level group.
REPO = "arraylake://earthmover-biology/RxRx19"

#: The OME-Zarr version of every document the ingest writes.
OME_VERSION = "0.6"

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


LICENSE = {"id": "CC-BY-4.0", "name": "Creative Commons Attribution 4.0 International",
           "url": "https://creativecommons.org/licenses/by/4.0/"}

#: The papers each dataset's README cites. Both READMEs cite both preprints.
PAPERS = [
    {"text": "Heiser et al. 2020, Identification of potential treatments for COVID-19 through artificial "
             "intelligence-enabled phenomic analysis of human cells infected with SARS-CoV-2",
     "doi": "10.1101/2020.04.21.054387"},
    {"text": "Cuccarese et al. 2020, Functional immune mapping with deep-learning enabled phenomics applied to "
             "immunomodulatory and COVID-19 drug discovery",
     "doi": "10.1101/2020.08.02.233064"},
]


def root_document() -> dict:
    return {
        "title": "RxRx19: Recursion's COVID-19 Cell Painting screens",
        "description": "RxRx19a and RxRx19b, published by Recursion Pharmaceuticals on rxrx.ai, with every image "
                       "referenced in place in Recursion's zips on gs://rxrx. Each dataset's group carries its "
                       "license, READMEs, citation and the choices this store made.",
        "license": LICENSE,
        "attribution": "RxRx19a and RxRx19b by Recursion Pharmaceuticals (https://www.rxrx.ai), CC BY 4.0",
        "source": BUCKET,
        "datasets": list(DATASETS),
    }


def dataset_document(dataset: Dataset, texts: dict) -> dict:
    """The dataset's group: what it is, its license and READMEs verbatim, and every choice the store made.

    ``texts`` holds the zip's ``LICENSE``, the README in the zip and the README
    in recursionpharma/rxrx-datasets at ``README_COMMIT``, read by
    ``source.license_and_readmes``.
    """
    folder = dataset.csv.split("/")[0].lower()
    page = f"https://www.rxrx.ai/{folder}"
    channels = (
        [{"file": f"w{i + 1}", "stain": stain} for i, stain in enumerate(dataset.stains)]
        if dataset.stains
        else "w1 … w6, as named in the file paths; no source maps them to stains"
    )
    pixel = (
        "The README says 0.65 micron/pixel. These PNGs are 1024-pixel images downsampled from 2048-pixel "
        "originals (per the dataset page), so the store takes their pixels as 1.30 µm; the README does not "
        "say which images its 0.65 describes."
        if dataset.pixel_size != 0.65
        else "The README says 0.65 micron/pixel; these PNGs are the instrument's 2048-pixel images."
    )
    return {
        "title": dataset.title,
        "publisher": "Recursion Pharmaceuticals",
        "page": page,
        "citation": PAPERS,
        "license": {**LICENSE, "text": texts["license"], "source": f"{dataset.images}, member {dataset.folder}/LICENSE"},
        "attribution": f"{dataset.folder} by Recursion Pharmaceuticals ({page}), CC BY 4.0",
        "source": {"images": dataset.images, "metadata": dataset.metadata, "embeddings": dataset.embeddings},
        "readme": {
            "text": texts["readme"],
            "url": README_URL.format(commit=README_COMMIT, folder=folder),
            "note": "The current README. It adds the stain table and the pixel size (October 2024) to the "
                    "README in the zip.",
        },
        "readme_in_zip": {"text": texts["readme_in_zip"], "source": f"{dataset.images}, member {dataset.folder}/README.md"},
        "pixel_size": {"value": dataset.pixel_size, "unit": "micrometer", "basis": pixel},
        "channels": channels,
        "known_errors": [{
            "where": "both READMEs, 'Well location on plate (column AA, row 2)'",
            "store": "AA02 is row AA, column 2: the letters run B–AE (30) and the numbers 02–47 (46), which fit "
                     "a 1536-well plate's 32 rows and 48 columns only that way",
        }],
        "arrays": {
            "embeddings": "embeddings.csv's deep-learning embeddings, one row per site, float32 (exact for the "
                          "CSV's values), rows in metadata.csv's order",
            "site_id": "the site each row of embeddings belongs to; also each site image's annotations.site_id",
        },
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
