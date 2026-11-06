"""DICOM pixel data as VirtualiZarr arrays that reference the bytes in the original instances.

    from minus80.parsers.dicom import wsi_levels

    levels = wsi_levels(["s3://bucket/series/a.dcm", "s3://bucket/series/b.dcm"])

Each source is a URL, a local path, or an open binary file (with ``url=``).
Writing stores is the job of an ingest, not this package.
"""
from .parser import dicom_json, multiplex_array, native_pixeldata_byte_range
from .volume import volume_array
from .wsi import encapsulated_frame_ranges, wsi_level_array, wsi_levels

__all__ = [
    "multiplex_array",
    "volume_array",
    "wsi_level_array",
    "wsi_levels",
    "encapsulated_frame_ranges",
    "dicom_json",
    "native_pixeldata_byte_range",
]
