"""Cut a window out of every level of an image pyramid, and say where each cut level sits.

The problem
-----------
An ingest sometimes stores part of a source image as an image of its own: one
field of view out of a well-sized mosaic, one core out of a tissue microarray
slide. Level 0 is easy, a window of the full-resolution array. The coarser
levels are not, if they are to be copied rather than recomputed:

- A level-0 window rarely starts on a coarse pixel. With 2 x 2 downsampling, a
  field starting at level-0 x = 6655 starts half way through coarse pixel
  3327 of level 1.
- So the window on a coarse level must cover every coarse pixel the field
  touches. It can be one pixel wider than the level-0 size divided by the
  factor, and its first pixel can start before the field does.
- A viewer can only line the levels up again if each level's metadata says
  exactly where its first pixel sits.

What this computes
------------------
For each level, :func:`crop_levels` returns the window to read and the
level's scale and translation, ready for
:func:`~minus80.ome.image_attrs` (``level_scales`` and ``level_translations``).

The convention, for a level whose pixels are ``f`` level-0 pixels wide:

- coarse pixel ``j`` stands for level-0 pixels ``j * f`` to ``j * f + f - 1``,
  which is what averaging or picking within ``f x f`` blocks produces;
- OME-Zarr places pixels by their centers, so coarse pixel ``j``'s center is
  at level-0 position ``j * f + (f - 1) / 2``;
- the cut image's own coordinates put level 0's first pixel center at 0, so a
  level's translation is its first pixel center minus the window's origin,
  in physical units.

An axis that is not cut (channel, or z when every plane is kept) is just a
window over its whole length with a factor of 1 and a translation of 0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CroppedLevel:
    """One level of a cut-out image: which part of the source level to read, and where it sits."""

    #: ``(start, stop)`` per axis, in the source level's pixels.
    window: dict[str, tuple[int, int]]
    #: Pixel size per axis, the source level's own.
    scale: dict[str, float]
    #: Where the first pixel's center sits, per axis, relative to level 0's
    #: first pixel center, in the same units as ``scale``.
    translation: dict[str, float]

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(stop - start for start, stop in self.window.values())

    def slices(self, dims: Sequence[str]) -> tuple[slice, ...]:
        """The window as an index for an array with axes ``dims``."""
        return tuple(slice(*self.window[d]) for d in dims)


def crop_levels(
    origin: Mapping[str, int],
    size: Mapping[str, int],
    level_shapes: Sequence[Sequence[int]],
    level_scales: Sequence[Mapping[str, float]],
) -> list[CroppedLevel]:
    """Where a window of level 0 lies on every level of a pyramid.

    ``origin`` and ``size`` are the window on level 0, in pixels, per axis.
    ``level_shapes`` and ``level_scales`` describe the source pyramid, one
    entry per level, with axes in the order ``origin`` lists them.

    Each level's factor is its pixel size over level 0's, and must be a whole
    number on every axis. A source that rounds its levels' sizes down can
    lack the last coarse pixel a window touches; the window then stops at the
    level's edge rather than raising.
    """
    dims = list(origin)
    base = level_scales[0]
    levels = []
    for k, (shape, scale) in enumerate(zip(level_shapes, level_scales)):
        window, translation = {}, {}
        for d, extent in zip(dims, shape):
            f = scale[d] / base[d]
            if abs(f - round(f)) > 1e-9:
                raise ValueError(
                    f"level {k}: axis {d} pixels are {f} level-0 pixels, not a whole number"
                )
            f = round(f)
            start = origin[d] // f
            stop = min(-(-(origin[d] + size[d]) // f), extent)
            if start >= stop:
                raise ValueError(
                    f"level {k}: the window on axis {d} lies outside the level's {extent} pixels"
                )
            window[d] = (start, stop)
            translation[d] = (start * f + (f - 1) / 2 - origin[d]) * base[d]
        levels.append(
            CroppedLevel(window=window, scale={d: scale[d] for d in dims}, translation=translation)
        )
    return levels
