"""Where the tensors of a safetensors file sit, for virtual references.

``safetensors.numpy.load`` decodes the values of a file but does not report where
each tensor lies in it, and a virtual reference to a tensor inside a larger file
(a STORED zip member, say) needs a byte offset and length. These functions read
the header, which is an 8-byte little-endian length followed by that many bytes
of JSON, and add the tensor offsets it records to the file's own position.
"""
from __future__ import annotations

import json
import struct
from typing import NamedTuple


class TensorRange(NamedTuple):
    dtype: str
    shape: tuple[int, ...]
    #: Byte range of the tensor within the file, counted from the file's first byte.
    start: int
    stop: int


def safetensors_header(data: bytes) -> dict:
    """The JSON header of a safetensors file: tensor names mapped to dtype, shape and ``data_offsets``.

    ``data_offsets`` are relative to the end of the header. An optional
    ``__metadata__`` entry is returned as it is.
    """
    (length,) = struct.unpack("<Q", data[:8])
    return json.loads(data[8:8 + length])


def tensor_ranges(data: bytes, file_offset: int = 0) -> dict[str, TensorRange]:
    """The byte range of each tensor in a safetensors file.

    ``data`` is the whole file, or at least its header. Ranges are counted from
    the file's first byte; pass ``file_offset``, the file's position inside a
    larger one such as a zip's ``ZipEntry.data_offset``, to get absolute ranges.
    """
    (length,) = struct.unpack("<Q", data[:8])
    body = file_offset + 8 + length
    return {
        name: TensorRange(spec["dtype"], tuple(spec["shape"]), body + spec["data_offsets"][0], body + spec["data_offsets"][1])
        for name, spec in safetensors_header(data).items()
        if name != "__metadata__"
    }
