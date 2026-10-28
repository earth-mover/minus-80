"""Loading what the exploration tools read: the zip's central directory and a few metadata documents per well.

Everything here fetches or reads from the cache; nothing here interprets the
layout (that is :mod:`layout`). Two inputs:

- The **central directory**, the zip's table of contents: one row per member
  with its name, local-header offset, sizes and CRC. Fetched once through
  :func:`minus80.parsers.zip.reading.cached_central_directory` and kept as
  Parquet. From a laptop in the United States the 178.7 MB fetch from EBI
  takes about 15 minutes; from AWS London, seconds.
- A well's **metadata**: the few documents that say what member names cannot,
  such as where each field of view sits and what each channel is called.
  Fetched by :func:`minus80.parsers.zip.reading.read_members` the first time
  a tool needs that well, about 50 small requests, and kept as JSON.

Why not open the zip with zarr? zarr reads metadata one document per request
as it walks the hierarchy, and a walk over the whole plate took 160 to 246 s
from EBI, mostly reading the empty skeletons of AnnData tables. The directory
already names every member, so only the documents below are needed.

The cache directory is ``$MINUS80_CACHE_DIR``, else ``~/.cache/minus80``.
"""

from __future__ import annotations

import functools
import json
import math
from pathlib import Path

import polars as pl
import pyarrow as pa
import zarr
from zarr.core.buffer import default_buffer_prototype
from zarr.storage import MemoryStore

from minus80.core.fetch import cache_dir
from minus80.parsers.fractal.plate import read_table
from minus80.parsers.zip.reading import cached_central_directory, read_members

from layout import CHUNK_PIXELS, LEVELS

#: The zip as EBI serves it. A staged copy works too: pass its URL as ``source``.
SOURCE = (
    "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/228/S-BIAD3228/Files/"
    "Multiplexeddata/AssayPlate_Greiner_655090.zarr.zip"
)
CACHE = Path(cache_dir()) / "zebrafish-3d-4i"
DIRECTORY_FILE = CACHE / "central-directory.parquet"


@functools.cache
def directory_table(source: str = SOURCE) -> pa.Table:
    """The cached central directory, read once per process.

    The size check against the archive is skipped, so the tools work offline
    once the directory has been fetched; the deposit does not change. The
    converter, which reads members at these offsets, keeps the check.
    """
    return cached_central_directory(source, DIRECTORY_FILE, check_size=False)


def load_central_directory(source: str = SOURCE) -> pl.DataFrame:
    """The central directory as a data frame, one row per member, in directory order."""
    return pl.from_arrow(directory_table(source))


def load_directory_facts(source: str = SOURCE) -> dict[str, int]:
    """The archive's size and where its central directory sits."""
    metadata = directory_table(source).schema.metadata
    return {key: int(metadata[key.encode()]) for key in ("archive_size", "directory_offset", "directory_size")}


def well_metadata_names(member_names: list[str], well: str, table_round: int) -> tuple[list[str], list[str]]:
    """The metadata documents and the ``FOV_ROI_table`` members a well's tools need.

    Documents: the well's ``.zattrs``, each round's ``.zattrs`` (channel
    names, colors and pixel sizes), each image level's ``.zarray`` (shapes),
    and the labels group's ``.zattrs`` (label names). Table members: every
    member of the ``FOV_ROI_table`` of round ``table_round``, which hold each
    field's position. The four rounds' tables agree, so one is enough.
    """
    documents = []
    for name in member_names:
        parts = name.split("/")
        is_well_attrs = name == f"{well}/.zattrs"
        is_round_attrs = len(parts) == 4 and parts[3] == ".zattrs"
        is_level_array = len(parts) == 5 and parts[4] == ".zarray"
        is_labels_attrs = name == f"{well}/1/labels/.zattrs"
        if is_well_attrs or is_round_attrs or is_level_array or is_labels_attrs:
            documents.append(name)
    table_prefix = f"{well}/{table_round}/tables/FOV_ROI_table/"
    table_members = [name for name in member_names if name.startswith(table_prefix)]
    return documents, table_members


def load_well_metadata(directory: pl.DataFrame, well: str, source: str = SOURCE) -> dict:
    """What a well's metadata says that its member names do not, fetched once and cached as JSON.

    Returns a dict with:

    - ``fields``: each field of view's name (``FOV_n``) and the position of
      its corner on the well image, in micrometers.
    - ``pixel_um``: the level-0 pixel size in y and x.
    - ``grid``: each level's size in source chunks, as (rows, columns).
    - ``channels``: each round's channel names. Round 3's differ between
      wells, so these always come from the well's own metadata.
    - ``documents``: every fetched metadata document, by member name.
    """
    cached = CACHE / "wells" / f"{well.replace('/', '-')}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    member_names = directory.filter(pl.col("name").str.starts_with(well + "/"))["name"].to_list()
    image_rounds = sorted({int(name.split("/")[2]) for name in member_names if name.split("/")[2].isdigit()})
    table_round = image_rounds[0]
    document_names, table_names = well_metadata_names(member_names, well, table_round)
    fetched = read_members(source, document_names + table_names, directory_table(source))
    documents = {name: json.loads(fetched[name]) for name in document_names}

    # The table is an AnnData group; zarr and anndata read it from memory, so
    # nothing here decodes its arrays by hand.
    prefix = f"{well}/{table_round}/tables/FOV_ROI_table/"
    buffer = default_buffer_prototype().buffer
    table_store = MemoryStore({name[len(prefix):]: buffer.from_bytes(fetched[name]) for name in table_names})
    positions = read_table(zarr.open_group(table_store, mode="r", zarr_format=2))

    multiscales = documents[f"{well}/{table_round}/.zattrs"]["multiscales"][0]
    pixel_um = multiscales["datasets"][0]["coordinateTransformations"][0]["scale"][-1]
    grid = []
    for level in range(LEVELS):
        shape = documents[f"{well}/{table_round}/{level}/.zarray"]["shape"]
        grid.append([math.ceil(shape[-2] / CHUNK_PIXELS), math.ceil(shape[-1] / CHUNK_PIXELS)])
    fields = [
        {"name": str(name), "x_micrometer": float(row["x_micrometer"]), "y_micrometer": float(row["y_micrometer"])}
        for name, row in positions.iterrows()
    ]
    fields.sort(key=lambda field: int(field["name"].removeprefix("FOV_")))
    channels = {
        str(round_): [channel["label"] for channel in documents[f"{well}/{round_}/.zattrs"]["omero"]["channels"]]
        for round_ in image_rounds
    }
    metadata = {"fields": fields, "pixel_um": pixel_um, "grid": grid, "channels": channels, "documents": documents}
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(metadata))
    return metadata


def load_member(name: str, source: str = SOURCE) -> bytes:
    """One member's uncompressed bytes, fetched by one range request."""
    return read_members(source, [name], directory_table(source))[name]
