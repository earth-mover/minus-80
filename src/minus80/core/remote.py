"""File objects over a remote file, for a parser to index it before a manifest exists.

A parser such as tifffile, h5py or nd2 is handed a seekable file object and
walks the file's own structures (its directories, its B-tree) to find where
every chunk lives. This module supplies that file object. Reading chunks
afterwards goes through VirtualiZarr's ``ManifestStore`` and does not use it.

The module is configuration on top of ``obspec_utils.readers.BlockStoreReader``
and obstore's ``HTTPStore``: a timeout, a retry backoff, a block size and a
cache size, each measured against a parser making thousands of small, partly
backward reads over one connection to a public archive. docs/upstream.md
tracks moving these defaults into ``obspec_utils``.
"""

from __future__ import annotations

import datetime
from typing import NamedTuple
from urllib.parse import unquote

from obspec_utils.readers import BlockStoreReader
from obstore.store import HTTPStore

from .registry import as_url, store_and_key, url_dir

#: obstore builds an HTTP store on object_store's defaults, which abandon a
#: request after three minutes. An index walk is thousands of sequential range
#: reads against one host over tens of minutes, so a request the library gives
#: up on throws away the whole walk rather than one read.
CLIENT_OPTIONS = {"timeout": "300s", "connect_timeout": "60s"}

#: The backoff is set rather than left at the library's, which starts at
#: 100 ms: against a server refusing connections outright, twenty retries on
#: that ladder are spent in eleven seconds and the walk dies anyway. Starting
#: at two seconds and climbing to a minute spends the same retries over a
#: quarter of an hour, which is the timescale a public archive recovers on.
RETRY_CONFIG = {
    "max_retries": 30,
    "retry_timeout": datetime.timedelta(minutes=20),
    "backoff": {"init_backoff": datetime.timedelta(seconds=2),
                "max_backoff": datetime.timedelta(seconds=60),
                "base": 2.0},
}

#: What one read-ahead block covers. Both file structures this has been
#: measured against want the same thing and for the same reason: the metadata
#: is small, scattered, and interleaved with the pixel data, so a large block
#: spends its time transferring pixels nobody asked for. An HDF5 chunk index is
#: a B-tree of roughly kilobyte nodes sitting between the chunks, and the
#: reader's 1 MB default made a 1,024-chunk walk forty times slower over the
#: network. A TIFF directory walk reads a few scattered kilobytes per page, and
#: 1 MB blocks moved 2 MB per file in 4 s where 64 KB blocks moved 0.5 MB in
#: 1 s. 64 KB is also comfortably above the median backward seek a TIFF walk
#: makes, which is about 37 KB.
BLOCK_SIZE = 64 * 1024

#: How many blocks a reader keeps. Larger for a walk that revisits a wide span
#: of the file, which an HDF5 B-tree does and a TIFF directory chain does not.
CACHED_BLOCKS = 256


class Remote(NamedTuple):
    """One remote file as an object store and a key within it."""

    store: object
    key: str


def patient_store(url: str) -> Remote:
    """The file's object store and key, configured to wait out a slow server.

    Only HTTP sources are rebuilt; anything else keeps the registry's own
    store, which is local or S3 and carries its own retry behaviour.
    """
    url = as_url(url)
    if not url.startswith(("http://", "https://")):
        store, key = store_and_key(url)
        return Remote(store, key)
    base = url_dir(url)
    store = HTTPStore.from_url(base.rstrip("/"), client_options=CLIENT_OPTIONS,
                               retry_config=RETRY_CONFIG)
    # The manifest records the URL as given, so the key is decoded here, at the
    # point it becomes an object-store key, and nowhere else.
    return Remote(store, unquote(url[len(base):]))


def reader(url: str, *, block: int = BLOCK_SIZE,
           cached_blocks: int = CACHED_BLOCKS) -> BlockStoreReader:
    """A seekable file object over the remote file that caches the blocks it fetches.

    A parser walking a file's structures seeks back into regions it has already
    read, because a pointer in a directory or an index node often refers to
    something written earlier. The cache keeps those seeks from refetching.
    """
    store, key = patient_store(url)
    return BlockStoreReader(store, key, block_size=block, max_cached_blocks=cached_blocks)
