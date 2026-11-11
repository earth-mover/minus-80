"""Reading RxRx19's zips: their central directories, the PNGs they list, and metadata.csv.

``ingest.py``, ``verify.py`` and ``diagrams.py`` read the source through
:func:`survey`, so all three see the same listing. Each zip's central directory
is read once and kept as Parquet (:func:`cached_central_directory`); after that
the listing costs no request. ``metadata.csv`` is one small member of the
metadata zip, read each time.

:func:`rebased` points a dataset at another copy of the bucket, such as the
fixture zips the tests build, so the same code reads either.
"""

from __future__ import annotations

import dataclasses
import struct
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv

import layout
from layout import BUCKET, Dataset
from minus80.core.fetch import cache_dir, read_range
from minus80.parsers.zip.directory import member_spans
from minus80.parsers.zip.reading import cached_central_directory, read_members

#: A PNG member's name: experiment, plate, well, site and channel.
PNG = r"^[^/]+/images/(?P<experiment>[^/]+)/Plate(?P<plate>\d+)/(?P<well>[A-Z]+\d+)_s(?P<site>\d+)_w(?P<channel>\d+)\.png$"


@dataclass
class Survey:
    """What one dataset's zips hold, from their central directories and the metadata CSV."""

    dataset: Dataset
    directory: pa.Table
    pngs: pd.DataFrame
    metadata: pd.DataFrame
    metadata_zip: pa.Table
    embeddings_zip: pa.Table
    png_header: dict

    @property
    def archive_size(self) -> int:
        return int(self.directory.schema.metadata[b"archive_size"])

    @property
    def directory_size(self) -> int:
        return int(self.directory.schema.metadata[b"directory_size"])

    @property
    def directory_offset(self) -> int:
        return int(self.directory.schema.metadata[b"directory_offset"])


def rebased(dataset: Dataset, root: str) -> Dataset:
    """``dataset`` with every URL moved from ``gs://rxrx/`` to ``root``."""
    move = lambda url: root.rstrip("/") + "/" + url.removeprefix(BUCKET)
    return dataclasses.replace(dataset, images=move(dataset.images), metadata=move(dataset.metadata),
                               embeddings=move(dataset.embeddings))


def cached(url: str, cache: Path | None = None) -> pa.Table:
    """The central directory of the zip at ``url``, kept as Parquet under ``cache``."""
    name = url.rsplit("/", 1)[-1].removesuffix(".zip")
    path = (cache or Path(cache_dir()) / "rxrx19") / f"{name}.central-directory.parquet"
    return cached_central_directory(layout.readable(url), path)


def check_local_extra(directory: pa.Table, key: str) -> None:
    """Stop unless every member's span is its 30-byte header, its name, 28 bytes of extra field and its data.

    Members sit back to back, so a member whose span matches has exactly that
    extra field; this proves the offset rule in ``layout.LOCAL_EXTRA`` for every
    member with no request.
    """
    spans = member_spans(directory).to_pandas()
    rows = directory.take(pa.array(spans.entry)).to_pandas()
    expected = 30 + rows.name.str.encode("utf-8").str.len() + layout.LOCAL_EXTRA + rows.compressed.astype("int64")
    wrong = rows.name[(spans.end - spans.start).astype("int64") != expected]
    if len(wrong):
        raise ValueError(f"{key}: {len(wrong)} members have another local extra field, such as {wrong.iloc[0]}")


def survey(dataset: Dataset, cache: Path | None = None) -> Survey:
    directory = cached(dataset.images, cache)
    check_local_extra(directory, dataset.key)
    parts = pc.extract_regex(directory["name"], PNG)
    keep = pc.is_valid(parts)
    pngs = pa.table(
        {
            "name": directory["name"].filter(keep),
            **{f: pc.struct_field(parts.filter(keep), f) for f in ("experiment", "plate", "well", "site", "channel")},
            "offset": directory["local_header_offset"].filter(keep),
            "bytes": directory["compressed"].filter(keep),
            "method": directory["method"].filter(keep),
        }
    ).to_pandas()
    for column in ("plate", "site", "channel"):
        pngs[column] = pngs[column].astype(int)
    pngs["offset"] = pngs.offset.astype("int64")
    pngs["bytes"] = pngs.bytes.astype("int64")
    pngs["data_offset"] = pngs.offset + 30 + pngs.name.str.encode("utf-8").str.len() + layout.LOCAL_EXTRA
    pngs["site_id"] = pngs.experiment + "_" + pngs.plate.astype(str) + "_" + pngs.well + "_" + pngs.site.astype(str)

    metadata_zip = cached(dataset.metadata, cache)
    csv = read_members(layout.readable(dataset.metadata), [dataset.csv], metadata_zip)[dataset.csv]
    metadata = pyarrow.csv.read_csv(pa.BufferReader(csv)).to_pandas()

    first = pngs.iloc[0]
    data = read_range(layout.readable(dataset.images), int(first.data_offset), int(first.data_offset) + 33)
    width, height, depth, color = struct.unpack(">IIBB", data[16:26])
    return Survey(
        dataset=dataset,
        directory=directory,
        pngs=pngs,
        metadata=metadata,
        metadata_zip=metadata_zip,
        embeddings_zip=cached(dataset.embeddings, cache),
        png_header={"signature": data[:8], "width": width, "height": height, "bit_depth": depth, "color_type": color},
    )
