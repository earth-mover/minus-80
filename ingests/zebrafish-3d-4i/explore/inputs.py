"""Loading what the exploration tools read: the zip's central directory, and a few metadata documents per well.

This module only loads; it interprets nothing (that is :mod:`layout`). Two
inputs:

- The **central directory**, the zip's table of contents: one row per member
  with its name, local-header offset, sizes and CRC. It is fetched once
  through :func:`minus80.parsers.zip.reading.cached_central_directory` and
  kept as Parquet. From a laptop in the United States the fetch from EBI took
  10 minutes; from AWS London, seconds.
- A well's **raw metadata**: the metadata documents a tool needs, as JSON,
  and the rows of its ``FOV_ROI_table``. They are fetched with
  :func:`minus80.parsers.zip.reading.read_members` the first time a tool
  needs that well, about 50 small requests, and kept as JSON. Only raw values
  are kept, so a change to :mod:`layout` never meets a stale derived value.

Why not open the zip with zarr? zarr reads metadata one document per request
as it walks the hierarchy, and a walk over the whole plate took 160 to 246 s
from EBI, mostly reading the empty skeletons of AnnData tables. The directory
already names every member, so only the documents below are fetched.

The cache directory is ``$MINUS80_CACHE_DIR``, else ``~/.cache/minus80``. Each
cache file is named by a hash of the source URL, so a staged copy of the zip
gets its own.
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import zarr
from zarr.core.buffer import default_buffer_prototype
from zarr.storage import MemoryStore

from minus80.core.fetch import cache_dir
from minus80.parsers.fractal.plate import read_table
from minus80.parsers.zip.reading import cached_central_directory, read_members

#: The zip as EBI serves it. A staged copy works too: pass its URL as ``source``.
SOURCE = (
    "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/228/S-BIAD3228/Files/"
    "Multiplexeddata/AssayPlate_Greiner_655090.zarr.zip"
)
CACHE = Path(cache_dir()) / "zebrafish-3d-4i"


def source_key(source: str) -> str:
    """A short name for a source URL, for naming its cache files."""
    return hashlib.sha256(source.encode()).hexdigest()[:12]


@functools.cache
def directory_table(source: str = SOURCE, *, check_version: bool = False) -> pa.Table:
    """The central directory, read from its Parquet file once per process.

    With ``check_version`` left off, no request is made once the file exists,
    so views that use only the directory work offline. Anything that then
    reads members at the directory's offsets asks for ``check_version=True``,
    which compares the file with a ``HEAD`` of the archive first.
    """
    path = CACHE / f"central-directory-{source_key(source)}.parquet"
    return cached_central_directory(source, path, check_version=check_version)


def directory_facts(source: str = SOURCE) -> dict[str, int]:
    """The archive's size and where its central directory sits, from the kept directory."""
    metadata = directory_table(source).schema.metadata
    return {key: int(metadata[key.encode()]) for key in ("archive_size", "directory_offset", "directory_size")}


def well_metadata_names(member_names: list[str], well: str) -> tuple[list[str], list[str]]:
    """The metadata documents and the ``FOV_ROI_table`` members to fetch for a well.

    Documents: the well's ``.zattrs``, each round's ``.zattrs`` (channel
    names, colors and pixel sizes), each image level's ``.zarray`` (shapes),
    and the labels group's ``.zattrs`` (label names). Table members: every
    member of the lowest-numbered round's ``FOV_ROI_table``, which hold each
    field's position and size. The four rounds' tables agree, so one is
    enough.
    """
    rounds = sorted({name.split("/")[2] for name in member_names if name.split("/")[2].isdigit()})
    table_prefix = f"{well}/{rounds[0]}/tables/FOV_ROI_table/"
    documents, table_members = [], []
    for name in member_names:
        parts = name.split("/")
        if name.startswith(table_prefix):
            table_members.append(name)
        elif (
            name == f"{well}/.zattrs"
            or (len(parts) == 4 and parts[3] == ".zattrs")
            or (len(parts) == 5 and parts[4] == ".zarray")
            or name == f"{well}/1/labels/.zattrs"
        ):
            documents.append(name)
    return documents, table_members


def load_well_metadata(well: str, source: str = SOURCE) -> dict:
    """A well's raw metadata, fetched once and kept as JSON.

    Returns ``documents`` (each fetched metadata document, by member name) and
    ``fov_table`` (one dict per row of the ``FOV_ROI_table``, its field name
    under ``name`` and every column as it is stored).
    """
    cached = CACHE / f"wells-{source_key(source)}" / f"{well.replace('/', '-')}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    directory = directory_table(source, check_version=True)
    member_names = [name for name in directory["name"].to_pylist() if name.startswith(well + "/")]
    document_names, table_names = well_metadata_names(member_names, well)
    fetched = read_members(source, document_names + table_names, directory)
    documents = {name: json.loads(fetched[name]) for name in document_names}

    # The table is an AnnData group; zarr and anndata read it from memory, so
    # nothing here decodes its arrays by hand.
    prefix = table_names[0][: table_names[0].index("FOV_ROI_table/") + len("FOV_ROI_table/")]
    buffer = default_buffer_prototype().buffer
    store = MemoryStore({name[len(prefix):]: buffer.from_bytes(fetched[name]) for name in table_names})
    table = read_table(zarr.open_group(store, mode="r", zarr_format=2))
    fov_table = [{"name": str(name), **{column: float(value) for column, value in row.items()}}
                 for name, row in table.iterrows()]

    raw = {"documents": documents, "fov_table": fov_table}
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(raw))
    return raw


def load_member(name: str, source: str = SOURCE) -> bytes:
    """One member's uncompressed bytes, fetched by one range request at a checked offset."""
    return read_members(source, [name], directory_table(source, check_version=True))[name]
