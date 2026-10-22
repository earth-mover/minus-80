"""One TIFF page as a VirtualiZarr array, from what tifffile has already parsed.

tifffile does the parsing: it walks the directories, knows each page's tile
grid, and records where every tile's bytes sit (``dataoffsets`` and
``databytecounts``). What is left is to restate that as a chunk manifest and to
name codecs a Zarr reader can decode the tiles with, and that is all this does.

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

import base64
import enum
from collections.abc import Mapping, Sequence

import numpy as np
import tifffile
from imagecodecs.zarr import Jpeg, Lzw
from tifffile.tifffile import jpeg_decode_colorspace
from virtualizarr.manifests import ChunkManifest, ManifestArray
from virtualizarr.manifests.manifest import MISSING_CHUNK_PATH
from virtualizarr.manifests.utils import create_v3_array_metadata

#: Tags whose content the manifest and codecs already carry: where each tile's
#: bytes are, and the JPEG tables every tile shares.
DATA_TAGS = frozenset({"TileOffsets", "TileByteCounts", "StripOffsets", "StripByteCounts",
                       "JPEGTables"})

_JPEG = 7
_LZW = 5
_NONE = 1


def page_codecs(page: tifffile.TiffPage) -> list[dict]:
    """The Zarr codec chain that decodes one of ``page``'s tiles or strips.

    A multi-sample page stores its samples interleaved, (y, x, s), and the
    array is declared (c, y, x) because OME-Zarr puts the channel axis before
    space; the leading ``transpose`` maps one to the other.

    JPEG's colour handling is tifffile's: the JPEG and output colour spaces come
    from ``jpeg_decode_colorspace``, the function tifffile's own decoder calls,
    so an RGB JPEG without a JFIF marker is not mistaken for YCbCr.
    """
    if page.predictor != 1:
        raise NotImplementedError(f"page {page.index}: predictor {page.predictor!r}")
    samples = page.samplesperpixel
    if samples > 1 and page.planarconfig != tifffile.PLANARCONFIG.CONTIG:
        raise NotImplementedError(f"page {page.index}: planar configuration {page.planarconfig!r}")
    head = [{"name": "transpose", "configuration": {"order": [1, 2, 0]}}] if samples > 1 else []
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


def page_array(page: tifffile.TiffPage, url: str, *, shape: Sequence[int] | None = None,
               attributes: Mapping | None = None) -> ManifestArray:
    """``page`` as a virtual array whose chunks are its own tiles or strips.

    ``url`` is where the file's bytes live, as the manifest should record it.

    ``shape`` is the image's (rows, columns[, samples]) when it is not the
    page's own. tifffile reports a smaller shape for some formats' reduced
    levels than their tags state (a Philips TIFF pads each level's width up to
    whole tiles); the tile grid must still be the page's, which is checked.

    A tile or strip stored with zero bytes becomes a missing chunk, read as the
    fill value 0.
    """
    shape = tuple(shape or page.shape)
    if len(shape) not in (2, 3):
        raise NotImplementedError(f"page {page.index}: shape {shape}")
    height, width = shape[:2]
    samples = shape[2] if len(shape) == 3 else 1
    if samples != page.samplesperpixel:
        raise ValueError(f"page {page.index}: {samples} samples in shape, {page.samplesperpixel} in the page")
    rows, cols = _chunk_shape(page, height)
    grid = (-(-height // rows), -(-width // cols))
    offsets = np.asarray(page.dataoffsets, dtype=np.uint64)
    lengths = np.asarray(page.databytecounts, dtype=np.uint64)
    if offsets.size != grid[0] * grid[1]:
        raise ValueError(f"page {page.index}: {offsets.size} tiles for a {grid} grid over {shape}")
    offsets, lengths = offsets.reshape(grid), lengths.reshape(grid)
    paths = np.full(grid, url, dtype=np.dtypes.StringDType())
    empty = lengths == 0
    paths[empty] = MISSING_CHUNK_PATH
    offsets[empty] = 0
    if samples > 1:
        array_shape, chunks, dims = (samples, height, width), (samples, rows, cols), ("c", "y", "x")
        paths, offsets, lengths = paths[None], offsets[None], lengths[None]
    else:
        array_shape, chunks, dims = (height, width), (rows, cols), ("y", "x")
    metadata = create_v3_array_metadata(
        shape=array_shape, data_type=page.dtype, chunk_shape=chunks, fill_value=0,
        codecs=page_codecs(page), dimension_names=dims, attributes=dict(attributes or {}))
    manifest = ChunkManifest.from_arrays(paths=paths, offsets=offsets, lengths=lengths)
    return ManifestArray(metadata=metadata, chunkmanifest=manifest)


def series_arrays(series: tifffile.TiffPageSeries, url: str) -> dict[str, ManifestArray]:
    """Every level of ``series`` as ``{"0": level 0, "1": level 1, ...}``, at tifffile's shapes."""
    return {str(i): page_array(level.keyframe, url, shape=level.shape)
            for i, level in enumerate(series.levels)}


def _jsonable(value):
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, np.generic):
        return value.item()
    return value


def page_tags(page: tifffile.TiffPage, *, exclude: Sequence[str] = ("ImageDescription",)) -> dict:
    """Every tag of ``page`` as JSON, by tag name, except where the tiles are.

    Enumerated values are written by name, rationals as ``[numerator,
    denominator]`` and byte strings as base64. ``exclude`` names further tags to
    leave out; by default the ImageDescription, which can be megabytes and is
    usually wanted on its own.
    """
    skip = DATA_TAGS | set(exclude)
    return {tag.name: _jsonable(tag.value) for tag in page.tags if tag.name not in skip}


def resolution_um(page: tifffile.TiffPage) -> tuple[float, float] | None:
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
