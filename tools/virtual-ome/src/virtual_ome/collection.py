"""A group holding many independent images.

A study is often a directory of unrelated specimens: different animals, different
slides, different days. They share no axis, so they are not one array — but a
root group with 205 children and no metadata is just a directory listing again,
and no reader knows the children are images.

OME-Zarr's container for this is ``bioformats2raw.layout``: the root declares the
layout, and an ``OME`` subgroup lists the member images in order. The spec allows
those list entries to be any group path, so the images keep the names a person
would recognise instead of being renumbered 0..204.
"""

from __future__ import annotations

from collections.abc import Sequence

from .image import DEFAULT_VERSION, VERSIONS, validate

#: The layout version the spec defines. OME-Zarr 0.6 marks the whole mechanism
#: transitional, so this is the sanctioned container today rather than forever.
LAYOUT = 3


def root_attrs(version: str = DEFAULT_VERSION) -> dict:
    """Attributes for the group that holds the images."""
    attrs = {"version": VERSIONS[version], "bioformats2raw.layout": LAYOUT}
    validate(attrs, version, model="bf2raw")
    return {"ome": attrs}


def series_attrs(paths: Sequence[str], version: str = DEFAULT_VERSION) -> dict:
    """Attributes for the ``OME`` subgroup listing the member images.

    Order is meaningful: it is the order a reader will present the series in.
    The spec also allows an ``OME/METADATA.ome.xml`` beside this describing the
    whole collection, but only as a SHOULD, and a Zarr store has nowhere natural
    to put a loose XML file; ``series`` covers the same need for readers that
    speak OME-Zarr rather than Bio-Formats.
    """
    return {"ome": {"version": VERSIONS[version], "series": list(paths)}}
