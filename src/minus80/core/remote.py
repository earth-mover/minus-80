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

from .registry import store_and_key

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


def reader(url: str, *, block: int = BLOCK_SIZE,
           cached_blocks: int = CACHED_BLOCKS) -> BlockStoreReader:
    """A seekable file object over the remote file that caches the blocks it fetches.

    A parser walking a file's structures seeks back into regions it has already
    read, because a pointer in a directory or an index node often refers to
    something written earlier. The cache keeps those seeks from refetching.
    """
    store, key = store_and_key(url, patient=True)
    return BlockStoreReader(store, key, block_size=block, max_cached_blocks=cached_blocks)
