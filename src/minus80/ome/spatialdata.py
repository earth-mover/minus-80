"""Attributes for the elements of a SpatialData store.

The key names are those SpatialData's writer uses at container format 0.2.
Writing them here keeps the ingest free of the spatialdata package.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence

import numpy as np

CONTAINER_VERSION = "0.2"
RASTER_VERSION = "0.3"
POINTS_VERSION = "0.2"
SHAPES_VERSION = "0.3"
TABLE_VERSION = "0.2"
RASTER_OME_VERSION = "0.5-dev-spatialdata"

#: GeoParquet geometry type codes that SpatialData's shapes reader expects.
_GEOMETRY_TYPES = {"POINT": 0, "POLYGON": 3}


#: SpatialData's placeholder unit, which it writes for an element's own pixel or input axes.
PLACEHOLDER_UNIT = "unit"


def _axes(names: Sequence[str], unit: str) -> list[dict]:
    # SpatialData's reader raises on a space axis without a unit.
    return [{"name": n, "type": "channel"} if n == "c" else {"name": n, "type": "space", "unit": unit}
            for n in names]


def _system(name: str, axes: Sequence[str], unit: str) -> dict:
    return {"name": name, "axes": _axes(axes, unit)}


def _transform(kind: str, body: dict, input_axes: Sequence[str],
               output_axes: Sequence[str], system: str, unit: str) -> dict:
    return {"type": kind, **body,
            "input": _system("".join(input_axes), input_axes, PLACEHOLDER_UNIT),
            "output": _system(system, output_axes, unit)}


def container_attrs() -> dict:
    return {"spatialdata_attrs": {"version": CONTAINER_VERSION}}


def scale(factors: Sequence[float], axes: Sequence[str], *, system: str = "global",
          unit: str = PLACEHOLDER_UNIT) -> dict:
    """``unit`` is the unit of the output system's space axes."""
    return _transform("scale", {"scale": list(factors)}, axes, axes, system, unit)


def affine(matrix: np.ndarray, input_axes: Sequence[str], output_axes: Sequence[str],
           *, system: str = "global", unit: str = PLACEHOLDER_UNIT) -> dict:
    # The spec omits the homogeneous last row.
    return _transform("affine", {"affine": np.asarray(matrix)[:-1].tolist()},
                      input_axes, output_axes, system, unit)


def sequence(transforms: Sequence[Mapping], axes: Sequence[str], *, system: str = "global",
             unit: str = PLACEHOLDER_UNIT) -> dict:
    return _transform("sequence", {"transformations": list(transforms)}, axes, axes, system, unit)


def raster_attrs(ome: Mapping, transform: Mapping) -> dict:
    """Group attributes of an image or labels element, from an ``image_attrs`` document."""
    ome = copy.deepcopy(dict(ome))
    ome["version"] = RASTER_OME_VERSION
    ome["multiscales"][0]["coordinateTransformations"] = [dict(transform)]
    return {"ome": ome, "spatialdata_attrs": {"version": RASTER_VERSION}}


def points_attrs(axes: Sequence[str], transform: Mapping, *,
                 instance_key: str | None = None, feature_key: str | None = None) -> dict:
    keys = {"instance_key": instance_key, "feature_key": feature_key}
    return {"encoding-type": "ngff:points", "axes": list(axes),
            "coordinateTransformations": [dict(transform)],
            "spatialdata_attrs": {"version": POINTS_VERSION,
                                  **{k: v for k, v in keys.items() if v is not None}}}


def shapes_attrs(geometry: str, transform: Mapping) -> dict:
    return {"encoding-type": "ngff:shapes", "axes": ["x", "y"],
            "coordinateTransformations": [dict(transform)],
            "spatialdata_attrs": {"version": SHAPES_VERSION,
                                  "geos": {"name": geometry, "type": _GEOMETRY_TYPES[geometry]}}}


def table_attrs(region: str, *, region_key: str, instance_key: str) -> dict:
    return {"spatialdata-encoding-type": "ngff:regions_table", "region": region,
            "region_key": region_key, "instance_key": instance_key, "version": TABLE_VERSION}
