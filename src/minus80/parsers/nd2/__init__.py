"""Nikon ND2 frames as VirtualiZarr chunk references. See README.md.

:func:`index_nd2` reads an ND2's chunk map and metadata and returns an
:class:`ND2Index`, whose methods turn the stage positions into
``ManifestArray``\\ s over the file's own bytes. The parsing is ``nd2``'s.
"""

from .parser import ND2Index, UnsupportedND2, index_nd2

__all__ = ["ND2Index", "UnsupportedND2", "index_nd2"]
