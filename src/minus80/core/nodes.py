"""Write many Zarr nodes at once, from documents encoded once per kind.

Why not ``zarr.create_hierarchy``
---------------------------------
A store of hundreds of thousands of images is mostly nodes whose metadata is
identical: every image's array has the same ``zarr.json``, and every image's
group differs from the next only in its name. ``zarr.create_hierarchy`` (zarr
3.4) encodes each node's metadata separately, about 0.13 ms a node, half of it
``to_buffer_dict`` and ``json.dumps`` on documents it has already encoded. On
one RxRx19a plate (12,093 nodes) it took 1.57 s; writing the same documents,
each kind encoded once, as bytes into the store took 0.34 s, about 30 µs a node.

The bytes are the ``zarr.json`` documents the Zarr v3 spec defines, and the
array documents come from zarr's own encoder (:func:`array_document`), so the
store is an ordinary Zarr store. What this skips is zarr's node-creation
machinery: nothing checks for an existing node, and no parent group is created
implicitly, so the caller writes every group, root included.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

import zarr
import zarr.storage
from zarr.core.buffer import default_buffer_prototype

from .aio import run_sync


def group_document(attributes: Mapping | None = None) -> bytes:
    """A group's ``zarr.json``, with ``attributes`` already JSON-safe."""
    return json.dumps({"zarr_format": 3, "node_type": "group", "attributes": dict(attributes or {})}).encode()


def array_document(**create_array_kwargs) -> bytes:
    """An array's ``zarr.json``, encoded by zarr from the arguments ``zarr.create_array`` takes."""
    scratch = zarr.create_array(zarr.storage.MemoryStore(), **create_array_kwargs)
    return scratch.metadata.to_buffer_dict(default_buffer_prototype())["zarr.json"].to_bytes()


def write_nodes(store, documents: Mapping[str, bytes]) -> None:
    """Write each node's ``zarr.json`` (``{node path: document}``) into ``store``, all at once."""
    buffer = default_buffer_prototype().buffer

    async def write_all() -> None:
        await asyncio.gather(*(store.set(f"{path}/zarr.json" if path else "zarr.json", buffer.from_bytes(document))
                               for path, document in documents.items()))

    run_sync(write_all())
