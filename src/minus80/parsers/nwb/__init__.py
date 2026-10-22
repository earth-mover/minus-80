"""Parse NWB (Neurodata Without Borders) files into VirtualiZarr references.

    from minus80.parsers.nwb import open_collection
    group = open_collection(["a.nwb", "b.nwb"])   # one ManifestGroup, a group per file

NWB is HDF5, so the parser is VirtualiZarr's ``HDFParser``. This package is the
NWB schema layer: which datasets virtualize (numeric leaves) and which are
carried natively (heap-backed strings, compound tables, scalars), and, in
:mod:`.datasets`, how a file's arrays map onto shared, indexed axes.
"""

from .datasets import file_datasets
from .parser import (
    native_parts,
    open_collection,
    open_one,
    virtual_arrays,
)

__all__ = [
    "file_datasets",
    "native_parts",
    "open_collection",
    "open_one",
    "virtual_arrays",
]
