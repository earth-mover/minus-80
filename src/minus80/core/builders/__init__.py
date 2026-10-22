"""Chunk-manifest builders for layouts VirtualiZarr has no parser for.

:mod:`~minus80.core.builders.flat_binary` covers an uncompressed array stored
contiguously, or at a fixed stride, after a header: an MRC volume, a SpikeGLX or
Open Ephys recording, an uncompressed NIfTI or NRRD image. It reads no bytes
itself. The caller reads the header with the format's own library and passes in
the shape, dtype, byte order and data offset.

For a format VirtualiZarr already reads (HDF5, NetCDF, TIFF, Zarr, GRIB,
Kerchunk), call its parser, such as ``virtualizarr.parsers.HDFParser``, instead
of adding a builder here. docs/upstream.md tracks ``flat_binary`` as a candidate
``FlatBinaryParser`` for VirtualiZarr.
"""
