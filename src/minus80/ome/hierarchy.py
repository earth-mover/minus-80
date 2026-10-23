"""Finding the images inside a study group, wherever the layout puts them.

A collection lists its member images in ``OME/series`` as paths relative to
the collection root, and the spec lets those paths be nested. IDR's own
conversions nest them two deep -- ``experimentA/<dataset>/<image>.ome.zarr`` --
so the study group's direct children are not the images, and anything that
walks the store has to follow the same rule the spec gives a reader.

An HCS plate declares its images differently: the plate lists its wells and
each well its fields. A study may hold several plates and collections, at any
depth, since the spec forbids one group from being both.

An image may also hold images derived from it, such as a projection, as
subgroups. The spec names no such subgroup, so the image lists them in its
:data:`DERIVED` attribute, and a reader finds them there rather than by
walking the tree.
"""

from __future__ import annotations

import zarr

OME = "OME"

#: The subgroup an image's label images live in, which the spec names.
LABELS = "labels"

#: An image's attribute naming the image subgroups derived from it, each with
#: a description of how: ``{"proj": "maximum over z"}``.
DERIVED = "derived_images"


def series(study) -> list[str]:
    """The image paths the collection declares, in its order."""
    try:
        return list(dict(study[OME].attrs)["ome"]["series"])
    except (KeyError, TypeError):
        return []


def plate_fields(study) -> list[str]:
    """The field images every plate under the study declares, through its wells."""
    found = []
    for path, node in study.members(max_depth=None):
        if not isinstance(node, zarr.Group):
            continue
        plate = (dict(node.attrs).get("ome") or {}).get("plate")
        if not isinstance(plate, dict):
            continue
        for well in plate.get("wells", []):
            try:
                ome = dict(node[well["path"]].attrs).get("ome") or {}
            except KeyError:
                continue
            for image in (ome.get("well") or {}).get("images", []):
                found.append(f"{path}/{well['path']}/{image['path']}")
    return found


def collection_series(study) -> list[str]:
    """The images every ``bioformats2raw.layout`` collection in the study lists, the root's included."""
    found = series(study)
    for path, node in study.members(max_depth=None):
        ome = dict(node.attrs).get("ome") if isinstance(node, zarr.Group) else None
        if isinstance(ome, dict) and "bioformats2raw.layout" in ome:
            found += [f"{path}/{p}" for p in series(node)]
    return found


def derived(study, images: list[str]) -> list[str]:
    """The derived images each of ``images`` lists in its :data:`DERIVED` attribute."""
    found = []
    for path in images:
        try:
            names = dict(study[path].attrs).get(DERIVED) or {}
        except KeyError:
            continue
        found += [f"{path}/{name}" for name in names]
    return found


def declared(study) -> list[str]:
    """Every image path the study declares: collection series, plate fields and derived images."""
    primary = collection_series(study) + plate_fields(study)
    return primary + derived(study, primary)


def multiscale_groups(study) -> dict[str, zarr.Group]:
    """Every group under the study that describes itself as an image.

    A label image is a multiscale image too, and the spec puts it inside its
    own image's ``labels`` group. Walking for ``multiscales`` alone would
    return a study's segmentations as members of the collection, so the
    ``labels`` subtree is skipped here rather than at each caller.
    """
    found = {}
    for path, node in study.members(max_depth=None):
        if (isinstance(node, zarr.Group) and path.rsplit("/", 1)[-1] != OME
                and f"/{LABELS}/" not in f"/{path}/"):
            ome = dict(node.attrs).get("ome")
            if isinstance(ome, dict) and "multiscales" in ome:
                found[path] = node
    return found


def images(study) -> dict[str, zarr.Group]:
    """The declared images that exist, keyed by their path in the collection.

    Declared but absent paths are the ``series matches`` gate's business, so
    they are skipped here rather than raised.
    """
    out = {}
    for path in declared(study):
        try:
            node = study[path]
        except KeyError:
            continue
        if isinstance(node, zarr.Group):
            out[path] = node
    return out
