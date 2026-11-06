"""Assemble a radiology series (CT, MR, PET), one slice per instance, into a ``(z, y, x)`` array."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
from virtualizarr.manifests import ManifestArray

from .parser import Source, _aligned, _common_spacing, _native_frame, _physical, _pixel_spacing, _stack


def volume_array(
    sources: Sequence[Source],
    *,
    urls: Sequence[str | None] | None = None,
    attributes: Mapping | None = None,
) -> ManifestArray:
    """Single-slice instances as one ``(z, y, x)`` array, each slice one chunk.

    Slices are ordered by ``ImagePositionPatient`` z when every slice states
    it, else by ``InstanceNumber``. The array carries ``z_positions`` when stated, and a
    ``scale``/``units`` in millimetres: z is the median step between positions,
    which tolerates a single irregular gap, and y and x are ``PixelSpacing``.
    An axis the series does not state has no scale.
    """
    frames = [_native_frame(s, u) for s, u in zip(sources, _aligned(urls, sources))]
    positions = [f.ds.get("ImagePositionPatient") for f in frames]
    if all(p is not None for p in positions):
        z = [float(p[2]) for p in positions]
    else:
        z = [None] * len(frames)
    numbers = [int(f.ds.get("InstanceNumber") or 0) for f in frames]
    order = sorted(range(len(frames)), key=lambda i: numbers[i] if z[0] is None else z[i])
    frames, z = [frames[i] for i in order], [z[i] for i in order]

    z_step = float(np.median(np.diff(z))) if z[0] is not None and len(z) > 1 else None
    spacing = _common_spacing([_pixel_spacing(f.ds) for f in frames], "slices")
    attrs = {**({"z_positions": z} if z[0] is not None else {}),
             **_physical({"z": z_step, **dict(zip("yx", spacing or (None, None)))})}
    return _stack(frames, ("z", "y", "x"), {**attrs, **(attributes or {})})
