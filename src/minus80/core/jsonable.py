"""Values as the strict JSON that Zarr attributes hold."""

from __future__ import annotations

import base64
import enum
import json
import math
from collections.abc import Mapping
from typing import Any

import numpy as np


def jsonable(value: Any) -> Any:
    """``value`` as strict JSON that a Zarr attribute and Icechunk's parser accept.

    - Mappings → dicts with ``str`` keys; lists, tuples and numpy arrays → lists.
    - numpy scalars → Python scalars; ``Enum`` → its name.
    - NaN and ±infinity → ``null``: strict JSON has no token for them.
    - ``bytes`` → a ``str`` when it decodes as UTF-8, else ``{"base64": ...}``.
    - Anything else JSON cannot hold (an HDF5 object reference) → ``str(value)``.

    Undecodable bytes become a dict rather than a string with replacement
    characters, which could not be told apart from real text.
    """
    if isinstance(value, enum.Enum):
        return value.name
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(raw).decode("ascii")}
    if isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return str(value)
    return value
