"""Where the Brain-Wide Map's files are, and how this ingest reads them.

``ingest.py`` holds the layout of the store and the parsing of each product.
This module holds the plumbing it leans on:

- the two hosts and their URLs: DANDI's content-addressed blobs and the IBL
  bucket's revision folders;
- IBL's own dataset listings (the ONE cache), which name every file of a
  release with its size and hash;
- the repo, opened with both hosts as virtual chunk containers.
"""

from __future__ import annotations

import functools
import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import numpy as np
import obstore

from minus80.core.fetch import cache_dir, read_range

#: DANDI serves every asset from one public bucket in us-east-2, under a key
#: named after the content's own id. The key never changes for a given
#: content, so a reference into it stays valid across dandiset versions.
DANDI_BLOBS = "s3://dandiarchive/blobs/"
DANDI_API = "https://api.dandiarchive.org/api"
DANDISET = "000409"
#: The published version the session list was read from. A draft can change
#: under the ingest; a published version cannot.
DANDI_VERSION = "0.260309.1324"

#: The IBL public bucket, in us-east-1. Every session lives at
#: ``data/<lab>/Subjects/<subject>/<date>/<number>/``.
IBL_BUCKET = "ibl-brain-wide-map-public"
IBL_DATA = f"s3://{IBL_BUCKET}/data/"

#: The two virtual chunk containers the store reads through.
CONTAINERS = [DANDI_BLOBS, IBL_DATA]


def say(line: str) -> None:
    """Print one progress line at once, with the time."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


#: How often a DANDI API request is tried, and the wait before the first retry,
#: doubling after each. The API answers a burst of requests with 429 or 5xx
#: now and then, and a run should outlast a minute of that.
DANDI_TRIES = 6
DANDI_BACKOFF_S = 2.0


def dandi_json(path: str) -> dict:
    for attempt in range(DANDI_TRIES):
        try:
            with urllib.request.urlopen(f"{DANDI_API}/{path}", timeout=60) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            transient = not isinstance(error, urllib.error.HTTPError) or error.code in (429, 500, 502, 503, 504)
            if not transient or attempt == DANDI_TRIES - 1:
                raise
            wait = DANDI_BACKOFF_S * 2**attempt
            say(f"  DANDI API {path}: {error}; retrying in {wait:.0f} s")
            time.sleep(wait)


@functools.cache
def dandi_assets() -> list[dict]:
    """Every asset of the published dandiset version: path, size, asset id."""
    cached = Path(cache_dir()) / f"dandi-{DANDISET}-{DANDI_VERSION}-assets.json"
    if cached.exists():
        return json.loads(cached.read_text())
    assets, page = [], f"dandisets/{DANDISET}/versions/{DANDI_VERSION}/assets/?page_size=1000"
    while page:
        listing = dandi_json(page)
        assets += listing["results"]
        page = listing["next"] and listing["next"].removeprefix(f"{DANDI_API}/")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(assets))
    return assets


@dataclass(frozen=True)
class DandiFile:
    path: str  # the asset path in the dandiset, which names subject and session
    url: str  # the s3:// blob the bytes live in
    https: str  # the same blob over HTTPS, for readers that cannot sign S3 requests
    size: int
    sha256: str


@functools.cache
def dandi_file(eid: str, desc: str) -> DandiFile:
    """The session's NWB file of one kind (``raw`` or ``processed``), looked up once per run."""
    matches = [a for a in dandi_assets() if f"ses-{eid}_desc-{desc}_" in a["path"]]
    if len(matches) != 1:
        raise LookupError(f"{len(matches)} {desc} NWB assets for session {eid}")
    asset = matches[0]
    meta = dandi_json(f"dandisets/{DANDISET}/versions/{DANDI_VERSION}/assets/{asset['asset_id']}/")
    blob = next(u for u in meta["contentUrl"] if ".s3.amazonaws.com/blobs/" in u)
    return DandiFile(
        path=asset["path"],
        url=DANDI_BLOBS + blob.split("/blobs/", 1)[1],
        https=blob,
        size=asset["size"],
        sha256=meta["digest"]["dandi:sha2-256"],
    )




@functools.cache
def one_cache(tag: str) -> dict:
    """IBL's listing of one release: its ``sessions`` and ``datasets`` tables.

    ONE, IBL's loader, reads the same zip. Each dataset row names its session,
    its path within the session, its revision folder, its size and its MD5
    hash, so the ingest knows every file of a release without listing the
    bucket.
    """
    import pandas as pd

    key = f"caches/alyx/{tag}/cache.zip"
    local = Path(cache_dir()) / "ibl-one-cache" / tag / "cache.zip"
    if not local.exists():
        local.parent.mkdir(parents=True, exist_ok=True)
        store = obstore.store.S3Store(IBL_BUCKET, region="us-east-1", skip_signature=True)
        local.write_bytes(bytes(obstore.get(store, key).bytes()))
    with zipfile.ZipFile(local) as z:
        tables = {name: pd.read_parquet(io.BytesIO(z.read(f"{name}.pqt")))
                  for name in ("sessions", "datasets")}
        tables["info"] = json.loads(z.read("cache_info.json"))
    return tables


def bucket_url(session_path: str, rel_path: str, dataset_id: str) -> str:
    """The s3:// URL of one dataset file in the IBL bucket.

    The bucket stores each file with its dataset id before the extension
    (``spikes.times.npy`` is ``spikes.times.<id>.npy``). Revision folders are
    named ``#2024-05-06#``, and ``#`` starts a URL fragment, so the path is
    percent-encoded.
    """
    stem, ext = rel_path.rsplit(".", 1)
    return IBL_DATA + quote(f"{session_path}/{stem}.{dataset_id}.{ext}")


def pinned_etag(url: str, md5: str) -> str:
    """The S3 ETag of an IBL file, checked against the MD5 IBL's listing gives.

    The store pins each ALF reference to this ETag, so a read fails if IBL
    replaces the file. A file uploaded in one part has its MD5 as its ETag;
    one uploaded in parts has an ETag of its own form (``<hex>-<parts>``),
    which only the HEAD can give.
    """
    from urllib.parse import unquote

    store = obstore.store.S3Store(IBL_BUCKET, region="us-east-1", skip_signature=True)
    etag = obstore.head(store, unquote(url.removeprefix(f"s3://{IBL_BUCKET}/")))["e_tag"]
    if "-" not in etag.strip('"') and etag.strip('"') != md5:
        raise ValueError(f"{url}: ETag {etag} differs from the MD5 {md5} in IBL's listing")
    return etag


def npy_header(url: str) -> tuple[tuple[int, ...], np.dtype, int]:
    """Shape, dtype and data offset of a ``.npy`` file, read with numpy's own header parser."""
    head = io.BytesIO(read_range(url, 0, 128))
    version = np.lib.format.read_magic(head)
    reader = (np.lib.format.read_array_header_1_0 if version == (1, 0)
              else np.lib.format.read_array_header_2_0)
    shape, fortran_order, dtype = reader(head)
    if fortran_order:
        raise ValueError(f"{url}: Fortran-ordered arrays are not chunked along their first axis")
    offset = head.tell()
    if offset > 128:
        raise ValueError(f"{url}: header longer than the 128 bytes read")
    return tuple(shape), dtype, offset




def open_repo(location: str):
    """The repo at a local path or ``arraylake://org/repo``, with both hosts as containers.

    A local repo is created on first use. An Arraylake repo must already have a
    container for each host, which an org admin sets up.
    """
    from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo

    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=CONTAINERS)
    return create_repo(location, [], prefixes=CONTAINERS, open_existing=True)
