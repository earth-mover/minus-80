"""A TIFF series level as a VirtualiZarr array, from what tifffile has already parsed.

tifffile walks the directories, groups pages into series and resolution
levels, and records where every tile's bytes sit (``dataoffsets`` and
``databytecounts``). This module restates that as a chunk manifest and names
codecs a Zarr reader can decode the tiles with.

The codecs are imagecodecs' own Zarr 3 codecs (``imagecodecs_jpeg``,
``imagecodecs_lzw``, registered by the ``imagecodecs-zarr`` package), so a
store written from this reads with icechunk, zarr, imagecodecs and
imagecodecs-zarr installed and nothing else.

Only the combinations that have been checked tile for tile against tifffile's
own decode are accepted: JPEG (with or without ``JPEGTables``, RGB or YCbCr),
LZW and uncompressed, single-sample or interleaved three-sample, no predictor.
Anything else raises, rather than writing references that decode wrong.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
import tifffile
from imagecodecs.zarr import Jpeg, Lzw
from tifffile.tifffile import jpeg_decode_colorspace
from virtualizarr.manifests import ManifestArray

from minus80.core.manifest import channel_last_transpose, manifest_array

_JPEG = 7
_LZW = 5
_NONE = 1

#: tifffile's axis codes that mean the same as an OME-Zarr axis, with that
#: axis's name and type. Any other code (``Q`` for "unknown", ``I`` for "image
#: sequence") names no physical axis, so the dimension is left unnamed and
#: untyped rather than given a made-up name or a guessed type.
_AXES = {"T": ("t", "time"), "C": ("c", "channel"), "Z": ("z", "space")}


def page_codecs(page: tifffile.TiffPage, *, leading: int = 0) -> list[dict]:
    """The Zarr codec chain that decodes one of ``page``'s tiles or strips.

    A multi-sample page stores its samples interleaved, (y, x, s), and the
    array is declared (c, y, x) because OME-Zarr puts the channel axis before
    space; the leading ``transpose`` maps one to the other. ``leading`` is the
    number of axes in front of (c, y, x), each of chunk length 1.

    JPEG's colour handling is tifffile's: the JPEG and output colour spaces come
    from ``jpeg_decode_colorspace``, the function tifffile's own decoder calls,
    so an RGB JPEG without a JFIF marker is not mistaken for YCbCr.
    """
    if page.predictor != 1:
        raise NotImplementedError(f"page {page.index}: predictor {page.predictor!r}")
    samples = page.samplesperpixel
    if samples > 1 and page.planarconfig != tifffile.PLANARCONFIG.CONTIG:
        raise NotImplementedError(f"page {page.index}: planar configuration {page.planarconfig!r}")
    head = [channel_last_transpose(3 + leading)] if samples > 1 else []
    if page.compression == _JPEG:
        colorspace, outcolorspace = jpeg_decode_colorspace(
            page.photometric, page.planarconfig, page.extrasamples, page.is_jfif)
        jpeg = Jpeg(tables=page.jpegtables, bitspersample=page.bitspersample,
                    colorspace_jpeg=colorspace, colorspace_data=outcolorspace)
        return head + [jpeg.to_dict()]
    if samples > 1:
        raise NotImplementedError(f"page {page.index}: {samples}-sample {page.compression!r}")
    serializer = {"name": "bytes", "configuration": {"endian": "little" if page.parent.byteorder == "<" else "big"}}
    if page.compression == _LZW:
        return [serializer, Lzw().to_dict()]
    if page.compression == _NONE:
        return [serializer]
    raise NotImplementedError(f"page {page.index}: compression {page.compression!r}")


def _chunk_shape(page: tifffile.TiffPage, height: int) -> tuple[int, int]:
    """One storage unit's (rows, columns): a tile, or a strip of the full width.

    A strip image whose last strip is shorter than the rest has no regular
    chunk grid, because that strip decodes to fewer rows than a chunk holds.
    """
    if page.is_tiled:
        return page.tilelength, page.tilewidth
    rows = min(page.rowsperstrip, height)
    if height % rows:
        raise NotImplementedError(
            f"page {page.index}: {height} rows in strips of {rows} leaves a short final strip")
    return rows, page.imagewidth


def level_array(level: tifffile.TiffPageSeries, url: str, *,
                attributes: Mapping | None = None) -> ManifestArray:
    """One resolution level of a tifffile series as a virtual array of its own tiles or strips.

    ``url`` is where the file's bytes live, as the manifest should record it.

    Each page is one (y, x) grid of chunks. The series' other axes index pages,
    one chunk per page along each. Interleaved samples become a ``c`` axis
    before ``y`` and ``x``. A page tifffile reports as absent, and a tile or
    strip stored with zero bytes, become missing chunks, read as the fill
    value 0.

    The level's shape is tifffile's, not the page's tags: tifffile reports a
    smaller shape for some formats' reduced levels than their tags state (a
    Philips TIFF pads each level's width up to whole tiles). The tile grid must
    still be the page's, which is checked.

    Each named axis's type goes in the ``dimensions`` attribute: ``t`` is
    time, ``c`` (a channel axis or interleaved samples) is channel, and ``z``,
    ``y`` and ``x`` are space. An unnamed axis has no entry.

    ``attributes`` are merged over what the page states: ``scale`` and
    ``units`` for ``y`` and ``x`` from the resolution tags, and
    ``channel_names`` for an RGB or YCbCr page. ``scale``, ``units`` and
    ``dimensions`` merge per dimension, so a caller's ``z`` keeps the page's
    ``y`` and ``x``.
    """
    page = level.keyframe
    samples = page.samplesperpixel
    planar = "YXS" if samples > 1 else "YX"
    axes = level.axes
    if not axes.endswith(planar) or (samples > 1 and "C" in axes):
        raise NotImplementedError(f"series {level.name!r}: axes {axes}")
    leading_axes = axes[: len(axes) - len(planar)]
    leading = tuple(level.shape[: len(leading_axes)])
    height, width = level.shape[len(leading_axes):][:2]
    pages = list(level.pages)
    if len(pages) != math.prod(leading):
        raise NotImplementedError(
            f"series {level.name!r}: {len(pages)} pages for leading shape {leading}")

    codecs = page_codecs(page, leading=len(leading))
    rows, cols = _chunk_shape(page, height)
    grid = (-(-height // rows), -(-width // cols))
    offsets = np.zeros((len(pages), *grid), dtype=np.uint64)
    lengths = np.zeros_like(offsets)
    for i, p in enumerate(pages):
        if p is None:
            continue
        if len(p.dataoffsets) != grid[0] * grid[1]:
            raise ValueError(f"page {p.index}: {len(p.dataoffsets)} tiles for a {grid} grid "
                             f"over {height}x{width}")
        offsets[i] = np.reshape(p.dataoffsets, grid)
        lengths[i] = np.reshape(p.databytecounts, grid)
    offsets = offsets.reshape(*leading, *grid)
    lengths = lengths.reshape(*leading, *grid)

    dims = [_AXES[a][0] if a in _AXES else None for a in leading_axes]
    types = {_AXES[a][0]: _AXES[a][1] for a in leading_axes if a in _AXES}
    if samples > 1:
        offsets, lengths = offsets[..., None, :, :], lengths[..., None, :, :]
        shape = (*leading, samples, height, width)
        chunks = (*(1,) * len(leading), samples, rows, cols)
        dims.append("c")
    else:
        shape = (*leading, height, width)
        chunks = (*(1,) * len(leading), rows, cols)
    dims += ["y", "x"]
    types |= {"c": "channel"} if samples > 1 else {}
    types |= {"y": "space", "x": "space"}
    stated = _stated(page) | {"dimensions": {d: {"type": types[d]} for d in dims if d in types}}
    return manifest_array(
        url, offsets, lengths, shape=shape, chunk_shape=chunks, dtype=page.dtype,
        codecs=codecs, dimension_names=dims, attributes=merge_attributes(stated, attributes))


def merge_attributes(base: Mapping, over: Mapping | None) -> dict:
    """``over`` merged into ``base``; ``scale``, ``units`` and ``dimensions`` merge per dimension."""
    out = dict(base)
    for key, value in (over or {}).items():
        out[key] = {**out.get(key, {}), **value} if key in ("scale", "units", "dimensions") else value
    return out


def _stated(page: tifffile.TiffPage) -> dict:
    """The physical metadata ``page``'s own tags state, as array attributes."""
    out: dict = {}
    if (um := _resolution_um(page)) is not None:
        out["scale"] = {"y": um[0], "x": um[1]}
        out["units"] = {"y": "micrometer", "x": "micrometer"}
    # The photometric tag states a colour model, not channel names; R, G and B
    # are what any viewer would call the three samples of an RGB model, and
    # libjpeg decodes YCbCr to RGB.
    if page.samplesperpixel == 3 and page.photometric in (
            tifffile.PHOTOMETRIC.RGB, tifffile.PHOTOMETRIC.YCBCR):
        out["channel_names"] = ["R", "G", "B"]
    return out


def _resolution_um(page: tifffile.TiffPage) -> tuple[float, float] | None:
    """The pixel size the resolution tags state, as (y, x) micrometres, or None.

    None unless ``XResolution``, ``YResolution`` and ``ResolutionUnit`` are all
    present, and None for a unit of ``NONE``: tifffile answers INCH for an absent
    unit, and one pixel per inch is 25400 micrometres, a plausible-looking
    number that no scanner meant.
    """
    if not all(t in page.tags for t in ("XResolution", "YResolution", "ResolutionUnit")):
        return None
    if page.resolutionunit == tifffile.RESUNIT.NONE:
        return None
    x, y = page.get_resolution(tifffile.RESUNIT.MICROMETER)
    return 1 / y, 1 / x
