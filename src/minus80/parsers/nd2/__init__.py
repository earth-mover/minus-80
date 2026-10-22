"""Address a Nikon ND2's frames where they lie, without copying them.

One entry point, :func:`index_nd2`. It reads an ND2's chunk map and metadata
chunks over a handful of bounded range requests and returns an
:class:`ND2Index`, which turns each stage position into a VirtualiZarr
``ManifestArray`` over the file's own bytes.

The parsing is ``nd2``'s throughout. Nothing here decodes an ND2 chunk.
"""

from .parser import ND2Index, UnsupportedND2, index_nd2

__all__ = ["ND2Index", "UnsupportedND2", "index_nd2"]
