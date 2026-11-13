"""Attributes for the elements of a SpatialData store.

The key names are those SpatialData's writer uses at container format 0.2.
Writing them here keeps the ingest free of the spatialdata package.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence

import numpy as np
import zarr

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
               output_axes: Sequence[str], system: str, unit: str,
               input_system: str | None = None, input_unit: str = PLACEHOLDER_UNIT) -> dict:
    return {"type": kind, **body,
            "input": _system(input_system or "".join(input_axes), input_axes, input_unit),
            "output": _system(system, output_axes, unit)}


def container_attrs() -> dict:
    return {"spatialdata_attrs": {"version": CONTAINER_VERSION}}


def scale(factors: Sequence[float], axes: Sequence[str], *, system: str = "global",
          unit: str, input_system: str | None = None, input_unit: str = PLACEHOLDER_UNIT) -> dict:
    """``unit`` is the unit of the output system's space axes.

    ``input_system`` and ``input_unit`` name the input system, for a transform that follows another in a sequence.
    """
    return _transform("scale", {"scale": list(factors)}, axes, axes, system, unit, input_system, input_unit)


def translation(offsets: Sequence[float], axes: Sequence[str], *, system: str = "global", unit: str,
                input_system: str | None = None, input_unit: str = PLACEHOLDER_UNIT) -> dict:
    """``unit`` is the unit of the output system's space axes; the arguments are those of :func:`scale`."""
    return _transform("translation", {"translation": list(offsets)}, axes, axes, system, unit, input_system, input_unit)


def affine(matrix: np.ndarray, input_axes: Sequence[str], output_axes: Sequence[str],
           *, system: str = "global", unit: str, input_system: str | None = None,
           input_unit: str = PLACEHOLDER_UNIT) -> dict:
    # The spec omits the homogeneous last row.
    return _transform("affine", {"affine": np.asarray(matrix)[:-1].tolist()},
                      input_axes, output_axes, system, unit, input_system, input_unit)


def sequence(transforms: Sequence[Mapping], axes: Sequence[str], *, system: str = "global",
             unit: str) -> dict:
    return _transform("sequence", {"transformations": list(transforms)}, axes, axes, system, unit)


def raster_attrs(ome: Mapping, transform: Mapping) -> dict:
    """Group attributes of an image or labels element, from an ``image_attrs`` document."""
    # SpatialData reads the 0.5 layout under its own label; a 0.6 document would lose its axes.
    if ome.get("version") != "0.5":
        raise ValueError(f"a SpatialData raster needs an OME-Zarr 0.5 document, not {ome.get('version')!r}")
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


def _write_dict(group: zarr.Group, name: str, values: Mapping[str, str]) -> None:
    """Write ``values`` as AnnData's on-disk dict of string scalars."""
    child = group.require_group(name)
    child.attrs.update({"encoding-type": "dict", "encoding-version": "0.1.0"})
    for key, value in values.items():
        array = child.create_array(key, shape=(), dtype=str, overwrite=True)
        array[()] = value
        array.attrs.update({"encoding-type": "string", "encoding-version": "0.2.0"})


def write_table_keys(group: zarr.Group, region: str, *, region_key: str, instance_key: str) -> None:
    """Write the table keys of an AnnData ``group`` to its attributes and to ``uns/spatialdata_attrs``.

    SpatialData reads the keys only from ``uns``; the group attributes mirror them for other readers.
    """
    _write_dict(group, "uns", {})
    _write_dict(group["uns"], "spatialdata_attrs",
                {"region": region, "region_key": region_key, "instance_key": instance_key})
    group.attrs.update(table_attrs(region, region_key=region_key, instance_key=instance_key))
