"""The plumbing under ``ingest.py``: IDC's bucket, its index, and the writes into a session.

``ingest.py`` holds what IDC publishes and how the store holds it. This module
holds what its steps need and none of them is about:

- :func:`series_objects`, the DICOM objects of one series in the bucket;
- :func:`local`, a local copy of one object, for a parser that walks it;
- :class:`Writer`, which writes groups, virtual and native arrays and
  documents into one Icechunk session;
- :func:`open_repository`, a local repo whose virtual chunks may read the bucket.
"""

from __future__ import annotations

import json
import os
import time

import numpy as np
import obstore
import zarr
from minus80.core.fetch import download_then_index
from minus80.core.icechunk_io import create_repo
from obstore.store import S3Store
from virtualizarr.manifests import ManifestArray, ManifestGroup

#: IDC's public AWS bucket (us-east-1, anonymous). Every series is a prefix
#: named by its ``crdc_series_uuid``, holding one object per instance.
BUCKET = "idc-open-data"
SOURCE = f"s3://{BUCKET}/"

os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")

BLOSC = zarr.codecs.BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")
ZSTD = zarr.codecs.ZstdCodec(level=5)


def say(line: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def series_objects(crdc_series_uuid: str) -> list[tuple[str, int]]:
    """``(url, size)`` of every DICOM object in one series' prefix."""
    store = S3Store(BUCKET, region="us-east-1", skip_signature=True)
    return [(f"{SOURCE}{o['path']}", int(o["size"]))
            for batch in obstore.list(store, prefix=f"{crdc_series_uuid}/")
            for o in batch if o["path"].endswith(".dcm")]


def local(url: str) -> str:
    """A local copy of ``url``, fetched once with parallel range GETs."""
    return download_then_index(url)


class Writer:
    """Writes groups, arrays and documents into one Icechunk session."""

    def __init__(self, session):
        self.session = session
        self.root = zarr.open_group(session.store, mode="a")

    def group(self, path: str, attrs: dict | None = None) -> zarr.Group:
        group = self.root.require_group(path) if path else self.root
        if attrs:
            group.attrs.update(attrs)
        return group

    def virtual(self, path: str, arrays: dict[str, ManifestArray], attrs: dict) -> None:
        self.group(path.rsplit("/", 1)[0])
        ManifestGroup(arrays=arrays, attributes=attrs).to_icechunk(self.session.store, group=path)

    def native(self, path: str, data: np.ndarray, *, dims, chunks=None, attrs=None,
               compressors=ZSTD) -> zarr.Array:
        parent, name = path.rsplit("/", 1)
        array = self.group(parent).create_array(
            name, shape=data.shape, chunks=chunks or data.shape or (1,), dtype=data.dtype,
            dimension_names=dims, attributes=attrs or {}, compressors=compressors, fill_value=0,
            config={"write_empty_chunks": False})
        if data.size:
            array[:] = data
        return array

    def document(self, path: str, value: dict) -> dict:
        """``value`` as UTF-8 JSON in a native byte array, and the pointer to it.

        A slide instance's DICOM JSON is about 150 KB, most of it the ICC
        profile, which is too large to repeat in a group's attributes.
        """
        data = np.frombuffer(json.dumps(value).encode(), np.uint8)
        self.native(path, data, dims=("byte",), attrs={"format": "DICOM JSON (PS3.18), UTF-8"})
        return {"array": path, "bytes": int(data.size), "format": "DICOM JSON (PS3.18), UTF-8"}


def open_repository(location: str):
    """A local repo at ``location`` whose virtual chunks may read IDC's bucket."""
    return create_repo(location, [], prefixes=[SOURCE], open_existing=True)
