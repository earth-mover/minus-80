"""Finding the images inside a study group, wherever the layout puts them.

A collection lists its member images in ``OME/series`` as paths relative to
the collection root, and the spec lets those paths be nested. IDR's own
conversions nest them two deep -- ``experimentA/<dataset>/<image>.ome.zarr`` --
so the study group's direct children are not the images, and anything that
walks the store has to follow the same rule the spec gives a reader.
"""

from __future__ import annotations

import zarr

OME = "OME"

#: The subgroup an image's label images live in, which the spec names.
LABELS = "labels"


def series(study) -> list[str]:
    """The image paths the collection declares, in its order."""
    try:
        return list(dict(study[OME].attrs)["ome"]["series"])
    except (KeyError, TypeError):
        return []


def multiscale_groups(study) -> dict[str, zarr.Group]:
    """Every group under the study that describes itself as an image.

    A label image is a multiscale image too, and the spec puts it inside its
    own image's ``labels`` group. Walking for ``multiscales`` alone would
    return a study's segmentations as members of the collection, so the
    ``labels`` subtree is skipped here rather than at each caller.
    """
    found = {}
    for path, node in study.members(max_depth=None):
        if isinstance(node, zarr.Group) and path != OME and f"/{LABELS}/" not in f"/{path}/":
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
    for path in series(study):
        try:
            node = study[path]
        except KeyError:
            continue
        if isinstance(node, zarr.Group):
            out[path] = node
    return out
