"""Parse uncompressed and tiled DICOM into VirtualiZarr references (no copy).

    from minus80.parsers.dicom import multiplex_array

    # N single-frame marker instances -> one (c, y, x) ManifestArray, names carried along
    marr, markers = multiplex_array(dcm_paths)

Reference the same bytes on a remote store instead of the local files with ``url_for``:

    multiplex_array(local_paths, url_for=lambda p: f"https://host/series/{Path(p).name}")

Writing stores is the job of an ingest, not this package.
"""
from .parser import (
    channel_metadata,
    dicom_json,
    multiplex_array,
    native_pixeldata_byte_range,
)
from .volume import slice_metadata, volume_array
from .wsi import encapsulated_frame_ranges, pyramid_levels, wsi_level_array

__all__ = [
    "multiplex_array",
    "volume_array",
    "wsi_level_array",
    "pyramid_levels",
    "encapsulated_frame_ranges",
    "dicom_json",
    "channel_metadata",
    "slice_metadata",
    "native_pixeldata_byte_range",
]
