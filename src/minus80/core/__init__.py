"""Shared code for turning a file's bytes into Icechunk chunk references.

Parsers and ingests call it; nothing here is specific to one format or archive.
For a format VirtualiZarr already parses (HDF5, NetCDF, TIFF, Zarr, GRIB), call
VirtualiZarr's parser directly. See README.md for a list of the modules.
"""
from .registry import as_url, registry_for

__all__ = ["as_url", "registry_for"]
