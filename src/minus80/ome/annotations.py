"""Per-image annotation that travels with the bytes.

In many studies what identifies an image (which animal, which slide, which day)
is encoded only in its filename, and a store that drops the filename loses it.
These functions write those fields twice: on each image, so opening one tells
you what it is, and as a table on the collection, so a reader can find an image
without opening all of them.

These live under their own top-level key rather than inside ``ome``. OME-Zarr
defines no place for study-specific fields, and putting unspecified keys inside
the ``ome`` namespace would make the document fail validation against a stricter
reader.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Top-level attribute key, kept out of the ``ome`` namespace.
KEY = "annotations"


def image_annotation(fields: Mapping[str, object], *, source_url: str | None = None) -> dict:
    """Attributes describing one image."""
    body = dict(fields)
    if source_url:
        body["source_url"] = source_url
    return {KEY: body}


def annotation_table(rows: Mapping[str, Mapping[str, object]]) -> dict:
    """A column-oriented table of every image's fields, for the collection root.

    Columns rather than rows so that scanning one field across the collection
    does not mean parsing every record. ``image`` holds the group paths, and
    every other column is aligned to it.

    This is JSON in the group attributes, which suits a study of a few hundred
    images. A collection large enough that the table itself is a burden wants
    real arrays instead.
    """
    paths = list(rows)
    columns = sorted({k for r in rows.values() for k in r})
    table: dict[str, list] = {"image": list(paths)}
    for col in columns:
        table[col] = [rows[p].get(col) for p in paths]
    return {KEY: table}
