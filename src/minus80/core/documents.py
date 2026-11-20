"""Verbatim source documents too large for a node's attributes, kept as byte arrays beside it.

A store keeps every document its source states: a scanner's ImageDescription,
a challenge's checksum list, a DICOM header. Small ones belong in the
attributes of the node they describe. A large one there makes every reader of
that node download and parse it: one Philips slide's ImageDescription is about
1 MB, most of it a base64 JPEG of the slide's overview photograph. So a
document over :data:`TEXT_LIMIT` is written as a native ``uint8`` array in a
``documents`` group beside the node, and the attribute holds a pointer to it.
"""

from __future__ import annotations

import numpy as np
import zarr

#: The largest document, in UTF-8 bytes, that is kept in attributes.
TEXT_LIMIT = 64 * 1024

#: The group of documents over the limit, a sibling of the nodes they belong to.
DOCUMENTS = "documents"

ZSTD = zarr.codecs.ZstdCodec(level=5)


def text_or_pointer(root: zarr.Group, parent: str, name: str, text: str, *, of: str, fmt: str,
                    limit: int = TEXT_LIMIT) -> str | dict:
    """``text`` itself when it fits in ``limit`` bytes, else a pointer to the array holding it.

    The array is ``<parent>/documents/<name>``, one document's UTF-8 bytes,
    with ``document_of`` (``of``) and ``format`` (``fmt``) as attributes. The
    pointer is ``{"array", "relative_to", "format", "bytes", "reason"}``, so a
    reader that finds a dict where text was expected knows where to look and
    why. ``parent`` is the path of the group the documents sit in, ``""`` for
    the root.
    """
    data = text.encode()
    if len(data) <= limit:
        return text
    path = f"{parent}/{DOCUMENTS}" if parent else DOCUMENTS
    group = root.require_group(path)
    group.attrs.update({"description": (
        f"Verbatim source documents too large for attributes (over {limit} bytes): each array "
        "is one document's UTF-8 bytes. Decode with bytes(array[:]).decode().")})
    values = np.frombuffer(data, dtype=np.uint8)
    group.create_array(
        name, shape=values.shape, chunks=values.shape, dtype=np.uint8, compressors=ZSTD,
        dimension_names=("byte",), fill_value=0, overwrite=True,
        attributes={"document_of": of, "format": fmt})[:] = values
    return {"array": f"{DOCUMENTS}/{name}", "relative_to": parent or "/", "format": fmt,
            "bytes": len(values), "reason": f"over the {limit}-byte limit for attributes"}


def read_text(root: zarr.Group, value: str | dict) -> str:
    """The document an attribute holds, following a :func:`text_or_pointer` pointer when it is one."""
    if isinstance(value, str):
        return value
    base = value["relative_to"].strip("/")
    path = f"{base}/{value['array']}" if base else value["array"]
    return bytes(root[path][:]).decode()
