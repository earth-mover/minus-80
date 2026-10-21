"""virtual-core: the small, shared layer under the bio-virtualization subprojects.

Deliberately thin. It holds only what VirtualiZarr does **not** already give you
for bio formats (see ../../UPSTREAM.md for the parts destined to be contributed back):

* ``builders.flat_binary`` -- virtualize a flat/headerless uncompressed array
  (MRC, raw ephys, ...) that VirtualiZarr has no parser for.
* ``fetch.download_then_index`` -- pull a remote file once via parallel range GETs
  so a parser can walk its metadata locally.
* ``icechunk_io`` -- create an Icechunk repo authorized for its sources, write a
  ManifestGroup to it, and reopen it.
* ``registry`` -- object-store registry glue for local + cloud sources.

For HDF5/NetCDF/TIFF/Zarr, use VirtualiZarr's own parsers directly -- not this.
"""
from .registry import as_url, registry_for

__all__ = ["as_url", "registry_for"]
