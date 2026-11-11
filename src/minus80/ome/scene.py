"""The ``ome`` attributes for an OME-Zarr 0.6 scene: images placed in one shared space.

A scene is a group above several images that declares a coordinate system of
its own and maps each image's intrinsic coordinate system into it. The fields
of view of a well, each its own image, are the case it was written for: the
scene says where each field sat when it was taken, so a viewer can lay the
well out as one picture without the pixels being pasted into one array.

> For images that share a spatial relationship, the ``scene`` metadata layout
> can be used to describe the relationship between images.
>
> -- OME-Zarr 0.6, https://ngff.openmicroscopy.org/0.6/index.html#scene-md

Only 0.6 defines scenes. A scene's group may also be a well: neither schema
forbids the other's key, so one ``ome`` block can carry both.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ome_zarr_models.v06 import coordinate_transforms as v06_ct
from ome_zarr_models.v06.scene import SceneAttrs

from .image import _DISCRETE, INTRINSIC, VERSIONS, _document, axis_types, check_axis_types

VERSION = "0.6"


def scene_attrs(
    translations: Mapping[str, Mapping[str, float]],
    *,
    name: str,
    dims: Sequence[str],
    units: Mapping[str, str] | None = None,
    dimensions: Mapping[str, Mapping] | None = None,
) -> dict:
    """Attributes for a group that places each image in ``translations`` in one coordinate system.

    ``translations`` maps each image's path, relative to the group, to where
    its intrinsic origin sits in the scene's coordinate system ``name``, per
    axis, in ``units``. Every image must share ``dims``. The scene's system is
    listed first, so a viewer displays it by default, as the spec recommends.

    Returns the whole attribute document, ``ome`` key included, as
    :func:`minus80.ome.well_attrs` does.
    """
    if not translations:
        raise ValueError("a scene needs at least one image")
    units = units or {}
    types = axis_types(dims, dimensions)
    check_axis_types(dims, types)
    axes = [
        v06_ct.Axis(name=d, type=t, unit=units.get(d), discrete=True if t in _DISCRETE else None)
        for d, t in zip(dims, types)
    ]
    transforms = []
    for path, shift in translations.items():
        if missing := set(dims) - set(shift):
            raise ValueError(f"{path}: no translation given for axes {sorted(missing)}")
        transforms.append(
            v06_ct.Translation(
                translation=tuple(float(shift[d]) for d in dims),
                input=v06_ct.CoordinateSystemIdentifier(name=INTRINSIC, path=path),
                output=v06_ct.CoordinateSystemIdentifier(name=name),
            )
        )
    scene = SceneAttrs(
        coordinateSystems=(v06_ct.CoordinateSystem(name=name, axes=tuple(axes)),),
        coordinateTransformations=tuple(transforms),
    )
    document = {"version": VERSIONS[VERSION], "scene": _document(scene)}
    return {"ome": document}
