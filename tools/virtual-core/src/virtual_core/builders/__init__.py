"""Archetype builders.

**Buy the parser.** For any format VirtualiZarr already reads -- HDF5/NetCDF, TIFF,
Zarr, GRIB, Kerchunk -- do NOT add a builder here. Call VirtualiZarr's parser
directly (e.g. ``virtualizarr.parsers.HDFParser``) or use the
`icechunk-datacube-ingestion` skill. Wrapping them would add nothing.

The one archetype VirtualiZarr does *not* cover, and the reason this package
exists, is the flat/headerless binary array:

* :mod:`~virtual_core.builders.flat_binary` -- one contiguous uncompressed array
  chunked by byte math from a header/sidecar. Covers MRC, SpikeGLX/Neuropixels,
  Open Ephys, uncompressed NIfTI/NRRD/LIF, .2bit.

This is a legitimate, general (non-bio) gap in VirtualiZarr -- it is staged here
for use across the bio subprojects and is a candidate upstream ``FlatBinaryParser``
contribution (see ../../../UPSTREAM.md). It parses no bytes itself: the caller uses
the format's own maintained library to read the header, then hands shape / dtype /
data-offset here.
"""
