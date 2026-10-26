"""Assemble OME-Zarr hierarchies over virtual (byte-range) arrays.

The arrays are written by VirtualiZarr; this supplies the metadata that turns
them into something a viewer can open, and nothing here reads or writes pixels.
"""

from .annotations import annotation_table, image_annotation
from .collection import root_attrs, series_attrs
from .image import (
    DEFAULT_VERSION,
    VERSIONS,
    axis_types,
    channel_omero,
    image_attrs,
    label_attrs,
    labels_attrs,
    rgb_omero,
    validate,
)
from .plate import plate_attrs, plate_grid, well_attrs

__all__ = [
    "DEFAULT_VERSION",
    "VERSIONS",
    "annotation_table",
    "axis_types",
    "channel_omero",
    "image_annotation",
    "image_attrs",
    "label_attrs",
    "labels_attrs",
    "plate_attrs",
    "plate_grid",
    "rgb_omero",
    "root_attrs",
    "series_attrs",
    "validate",
    "well_attrs",
]
