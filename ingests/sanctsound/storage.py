"""Where the SanctSound ingest reads its bytes from and writes them to.

NOAA's bucket and its path conventions, the text attributes of NOAA's netCDF
files, the chunk table a scan saves, and the Icechunk repo. ``ingest.py`` holds
the pipeline and the parsing of NOAA's data.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

import numpy as np
import obstore
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import GCSStore

from minus80.core.fetch import download_then_index
from minus80.core.registry import CLIENT_OPTIONS, RETRY_CONFIG
from minus80.core.remote import reader
from minus80.parsers.flac import ChunkTable, FrameScan, scan_frames


BUCKET = "noaa-passive-bioacoustic"

BASE = f"gs://{BUCKET}/"

GCS = f"https://storage.googleapis.com/{BUCKET}/"


# NOAA's bucket allows anonymous reads. The retry settings are core's, for a
# public archive that drops connections.
BUCKET_STORE = GCSStore(BUCKET, skip_signature=True, client_options=CLIENT_OPTIONS,
                        retry_config=RETRY_CONFIG)

BUCKET_REGISTRY = ObjectStoreRegistry({BASE: BUCKET_STORE})


def list_keys(prefix: str, suffix: str) -> list[tuple[str, int]]:
    """Every key under ``prefix`` that ends in ``suffix``, with its size in bytes."""
    keys = []
    for batch in obstore.list(BUCKET_STORE, prefix=prefix):
        for item in batch:
            if item["path"].endswith(suffix):
                keys.append((item["path"], item["size"]))
    return sorted(keys)


def read_object(key: str) -> bytes:
    """The whole object, fetched in 16 MB ranges.

    Product files run to 600 MB, and a single long GET is cut short often enough
    to matter. Each range is retried on its own, so a dropped connection costs
    one range.
    """
    return reader(BASE + key, registry=BUCKET_REGISTRY, block=16 << 20,
                  cached_blocks=1).read()


# Where recordings are downloaded for scanning. /dev/shm is held in memory, so
# the VM's disk does not limit the scan; it exists on Linux only.
SCAN_SCRATCH = "/dev/shm/sanctsound" if os.path.isdir("/dev/shm") else None


def scan_recording(key: str) -> FrameScan:
    """Every FLAC frame of one recording, found by downloading it, then parsing.

    The download uses parallel range requests and the parse reads a local
    file, as docs/formats.md describes; parsing while fetching leaves each
    process waiting on one request at a time. PyAV is never handed the URL,
    because the FFmpeg in PyAV's Linux wheels fails on HTTPS with
    "Input/output error". The references record the ``gs://`` URL, and the
    local copy is deleted once scanned.
    """
    path = download_then_index(BASE + key, SCAN_SCRATCH, registry=BUCKET_REGISTRY)
    try:
        return scan_frames(path, url=BASE + key)
    finally:
        os.remove(path)


def deployment_prefix(site: str, dep: str) -> str:
    return f"sanctsound/audio/{site.lower()}/sanctsound_{dep.lower()}/"


def product_files(product: str, site: str, dep: str) -> dict[str, str]:
    """A deployment's files of one NOAA product, keyed by label.

    ``.../SanctSound_CI01_01_BB_1h.nc`` has the label ``BB_1h``.
    """
    files = {}
    for key, _ in list_keys(f"sanctsound/products/{product}/{site.lower()}/", ".nc"):
        if f"_{dep}_" in key:
            label = key.split("/")[-1].removesuffix(".nc").removeprefix(f"SanctSound_{dep}_")
            files[label] = key
    return files


def text_attributes(node) -> dict[str, str]:
    """The attributes of a node in one of NOAA's netCDF files, as text.

    h5py returns some text attributes as bytes, which are decoded. NOAA writes
    ``history`` as a one-element Python list literal, which is unwrapped.
    Attributes starting with an underscore are netCDF's own bookkeeping and are
    left out.
    """
    attrs = {k: v.decode() if isinstance(v, bytes) else str(v)
             for k, v in node.attrs.items() if not k.startswith("_")}
    history = attrs.get("history", "")
    if history.startswith('["') and history.endswith('"]'):
        attrs["history"] = history[2:-2]
    return attrs


@dataclass(frozen=True)
class DeploymentTable:
    """A deployment's chunk table with what the parser's table does not carry.

    ``start`` is each recording's start time in nanoseconds since the epoch, from
    its filename. ``windows`` and ``n_listed`` are set when only the recordings
    around some time windows were scanned.
    """

    chunks: ChunkTable
    start: np.ndarray
    windows: tuple[str, ...] = ()
    n_listed: int | None = None

    def save(self, path) -> None:
        """Write the parser's table fields plus ``start`` to one ``.npz``."""
        extra = dict(windows=np.array(self.windows), n_listed=self.n_listed) \
            if self.windows else {}
        np.savez_compressed(path, **asdict(self.chunks), start=self.start, **extra)

    @classmethod
    def load(cls, path) -> DeploymentTable:
        """Read a saved table. A file with no ``channels`` field is mono.

        ``ci01_01_chunks.npz`` and its siblings predate the ``channels`` field,
        and every SanctSound recording is mono.
        """
        with np.load(path, allow_pickle=False) as z:
            fields = {k: z[k] for k in ChunkTable.__dataclass_fields__ if k in z}
            fields.setdefault("channels", 1)
            fields["sample_rate"] = int(fields["sample_rate"])
            fields["blocksize"] = int(fields["blocksize"])
            fields["channels"] = int(fields["channels"])
            windows = tuple(str(w) for w in z["windows"]) if "windows" in z else ()
            return cls(ChunkTable(**fields), z["start"].astype("int64"), windows,
                       int(z["n_listed"]) if "n_listed" in z else None)


def write_small_array(group, name, data, dims, **attrs):
    """Write a small materialized array in one chunk."""
    data = np.asarray(data)
    return group.create_array(name, data=data, chunks=data.shape or "auto",
                              dimension_names=dims, attributes=attrs, overwrite=True)


def repository(location: str, *, create: bool = False):
    """The Icechunk repo at ``location``: a directory, or ``arraylake://<org>/<repo>``.

    A local repo gets a virtual chunk container for NOAA's bucket. Arraylake repos
    hold their own containers, and this never changes them.
    """
    from minus80.core.icechunk_io import (ARRAYLAKE, arraylake_repository, create_repo,
                                          open_repository)

    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[BASE])
    if create:
        return create_repo(location, [], prefixes=[BASE], open_existing=True)
    return open_repository(location)
