"""Reading a remote file's own metadata, before there is a manifest to read it by.

Virtualizing a file happens in two phases, and they want opposite things from
the network.

**Indexing** comes first. A parser -- tifffile, h5py, nd2 -- is handed a
seekable file-like object and walks the file's own structures, its directories
or its B-tree, to find out where every chunk lives. No manifest exists yet.
That is what this module serves.

**Reading** comes afterwards. VirtualiZarr's ``ManifestStore`` is a zarr store
that resolves entries in a manifest that already exists to byte ranges through
obstore. Different job, different consumer, different time. This module is not
a slower version of it and does not replace it.

What is actually here is thin, and it is configuration rather than mechanism:
an HTTP store, a retry ladder, a block size and a cache, all four sitting on
top of somebody else's reader. Each is a measured default rather than a guess,
and each is measured against the same thing -- a parser making thousands of
small, partly backward reads over one connection to a public archive.

That generality is the point. None of it is specific to one archive or one
format, and two formats in this repo grew the same four settings independently
before they were put here. Its eventual home is ``obspec_utils``: a 1 MB
read-ahead default is a trap for any chunked remote read, and a cached reader
with a patient backoff is the general fix rather than a bioimaging one. See
``UPSTREAM.md``.
"""

from __future__ import annotations

import contextlib
import datetime
from typing import NamedTuple
from urllib.parse import unquote

from obspec_utils.readers import BlockStoreReader
from obstore.store import HTTPStore
from obspec_utils.registry import ObjectStoreRegistry

from .fetch import download_then_index
from .registry import as_url, registry_for, store_and_key, url_dir

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
    """One remote file, addressed three ways because three callers need it so."""

    store: object
    key: str
    registry: object


def patient_store(url: str) -> Remote:
    """The file's object store and key, configured to wait out a slow server.

    Only HTTP sources are rebuilt; anything else keeps the registry's own
    store, which is local or S3 and carries its own retry behaviour.
    """
    url = as_url(url)
    if not url.startswith(("http://", "https://")):
        store, key = store_and_key(url)
        return Remote(store, key, registry_for(url))
    base = url_dir(url)
    store = HTTPStore.from_url(base.rstrip("/"), client_options=CLIENT_OPTIONS,
                               retry_config=RETRY_CONFIG)
    # The manifest records the URL as given, so the key is decoded here, at the
    # point it becomes an object-store key, and nowhere else.
    return Remote(store, unquote(url[len(base):]), ObjectStoreRegistry({base: store}))


def reader(url: str, *, block: int = BLOCK_SIZE,
           cached_blocks: int = CACHED_BLOCKS) -> BlockStoreReader:
    """A seekable file-like over the remote file, caching what it has fetched.

    The cache is the point. A parser walking a file's structures seeks
    backwards into regions it has already passed, because a pointer inside a
    directory or an index node routinely refers to something written earlier.
    An uncached reader refetches on each of those.
    """
    store, key, _ = patient_store(url)
    return BlockStoreReader(store, key, block_size=block, max_cached_blocks=cached_blocks)


@contextlib.contextmanager
def buffered(url: str, *, block: int = BLOCK_SIZE,
             cached_blocks: int = CACHED_BLOCKS):
    """:func:`reader` as a context manager, for a parser that wants ``with``.

    Right when bandwidth to the source is the scarce thing: from a laptop, or
    anywhere outside the source's region. The gigabytes of pixel data are never
    touched.
    """
    yield reader(url, block=block, cached_blocks=cached_blocks)


def downloaded(cache_dir: str):
    """An opener that fetches the whole file once, then walks it locally.

    Right in-region, where bandwidth is plentiful and round trips are what
    costs: a file that scatters hundreds of directories through itself pays
    hundreds of sequential round trips under :func:`buffered` and one bulk
    transfer here. ``cache_dir`` is reused, so a re-run costs nothing.
    """

    @contextlib.contextmanager
    def opener(url: str):
        with open(download_then_index(url, cache_dir), "rb") as f:
            yield f

    return opener
