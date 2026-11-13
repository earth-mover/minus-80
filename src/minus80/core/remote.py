"""File objects over a remote file, for a parser to index it before a manifest exists.

A parser such as tifffile, h5py or nd2 is handed a seekable file object and
walks the file's own structures (its directories, its B-tree) to find where
every chunk lives. This module supplies that file object. Reading chunks
afterwards goes through VirtualiZarr's ``ManifestStore`` and does not use it.

The module is configuration on top of ``obspec_utils.readers.BlockStoreReader``:
a block size and a cache size, each measured against a parser making thousands
of small, partly backward reads over one connection to a public archive, read
through the registry's patient store (:data:`~minus80.core.registry.CLIENT_OPTIONS`,
:data:`~minus80.core.registry.RETRY_CONFIG`). docs/upstream.md tracks moving
these defaults into ``obspec_utils``.
"""

from __future__ import annotations

from obspec_utils.readers import BlockStoreReader
from obspec_utils.registry import ObjectStoreRegistry

from .registry import resolve, store_and_key

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


def reader(url: str, *, registry: ObjectStoreRegistry | None = None, block: int = BLOCK_SIZE,
           cached_blocks: int = CACHED_BLOCKS, wrap=None) -> BlockStoreReader:
    """A seekable file object over the remote file that caches the blocks it fetches.

    A parser walking a file's structures seeks back into regions it has already
    read, because a pointer in a directory or an index node often refers to
    something written earlier. The cache keeps those seeks from refetching.

    Reads go through ``registry``'s store for ``url`` when one is given, so
    its timeouts and retries are the caller's; otherwise through the patient
    store :func:`~minus80.core.registry.store_and_key` builds. ``wrap``, when
    given, takes that store and returns the store the reader reads through.
    """
    store, key = resolve(registry, url) if registry is not None else store_and_key(url, patient=True)
    return BlockStoreReader(wrap(store) if wrap else store, key, block_size=block, max_cached_blocks=cached_blocks)


class ReadCounter:
    """The bytes the store served, its call count and the progress line those bytes advance.

    ``requests`` counts store calls, not HTTP requests: one ``get_ranges`` call can send several.
    """

    def __init__(self):
        self.bytes, self.requests, self.progress = 0, 0, None

    def wrap(self, store) -> _CountedStore:
        return _CountedStore(store, self)

    def _count(self, size: int) -> None:
        # One store call; ``head`` is a call of 0 bytes.
        self.requests += 1
        self.bytes += size
        if size and self.progress is not None:
            self.progress.advance(size)


class _CountedStore:
    """A store that reports each read it serves to a :class:`ReadCounter`."""

    def __init__(self, store, counter: ReadCounter):
        self._store, self._counter = store, counter

    def get(self, path, **kwargs):
        result = self._store.get(path, **kwargs)
        start, end = result.range
        self._counter._count(end - start)
        return result

    def get_range(self, path, **kwargs):
        data = self._store.get_range(path, **kwargs)
        self._counter._count(len(data))
        return data

    def get_ranges(self, path, **kwargs):
        data = self._store.get_ranges(path, **kwargs)
        self._counter._count(sum(len(d) for d in data))
        return data

    def head(self, path):
        self._counter._count(0)
        return self._store.head(path)

    def __getattr__(self, name):
        return getattr(self._store, name)


def counting_reader(url: str, *, registry: ObjectStoreRegistry | None = None) -> tuple[BlockStoreReader, ReadCounter]:
    """:func:`reader` and the :class:`ReadCounter` for every read it makes.

    The counter wraps the store before the function builds the reader, so it sees every block the reader fetches.
    """
    counter = ReadCounter()
    return reader(url, registry=registry, wrap=counter.wrap), counter
