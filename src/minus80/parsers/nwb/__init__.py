"""Parse NWB (Neurodata Without Borders) files into VirtualiZarr references.

    from minus80.parsers.nwb import open_collection, file_datatree
    group = open_collection(["a.nwb", "b.nwb"])   # one ManifestGroup, a group per file
    tree = file_datatree("a.nwb")                 # one file, on shared, indexed axes

NWB is HDF5, so the parsing is VirtualiZarr's ``HDFParser``. This package calls
it group by group, reads natively what it cannot represent, and, in
:mod:`.datasets`, maps a file's arrays onto shared, indexed axes.
"""

from .datasets import file_datatree
from .parser import open_collection, virtual_arrays

__all__ = ["file_datatree", "open_collection", "virtual_arrays"]
