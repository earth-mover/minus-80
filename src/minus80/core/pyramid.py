"""Native pyramid levels: TIFF pages decoded once per tile and written as zarr chunks."""

import xml.etree.ElementTree as ET
from collections.abc import Iterator, Sequence

import numpy as np
import tifffile
import zarr
from zarr.codecs import BloscCodec

IMAGE_CODECS = (BloscCodec(cname="zstd", clevel=5, shuffle="shuffle"),)


def create_level(
    group: zarr.Group,
    name: str,
    shape: Sequence[int],
    dtype,
    *,
    dims: Sequence[str],
    chunk: tuple[int, int] = (1024, 1024),
    shard_planes: int | None = None,
) -> zarr.Array:
    """Create one level with one chunk per plane tile; `shard_planes` groups planes along z into a shard."""
    chunks = (1,) * (len(shape) - 2) + tuple(chunk)
    shards = (1,) * (len(shape) - 3) + (shard_planes, *chunk) if shard_planes else None
    return group.create_array(
        name,
        shape=tuple(shape),
        dtype=dtype,
        chunks=chunks,
        shards=shards,
        compressors=IMAGE_CODECS,
        dimension_names=tuple(dims),
    )


def _tiles(page: tifffile.TiffPage) -> Iterator[tuple[int, int, int, np.ndarray]]:
    """Yield `(channel0, y0, x0, tile)` for each decoded tile, cropped to the page, as `(s, h, w)`."""
    for segment, (sample, _, y0, x0, _), _ in page.segments():
        if segment is None:
            continue
        tile = segment[0, : page.imagelength - y0, : page.imagewidth - x0]
        yield sample, y0, x0, np.moveaxis(tile, -1, 0)


def _region(page: tifffile.TiffPage, index, channel0, y0, x0, tile):
    """Index into the array that receives `tile`; a one-sample page has no channel axis."""
    spatial = (slice(y0, y0 + tile.shape[1]), slice(x0, x0 + tile.shape[2]))
    if page.samplesperpixel == 1:
        return index + spatial
    return index + (slice(channel0, channel0 + tile.shape[0]), *spatial)


def _check_shape(array: zarr.Array, page: tifffile.TiffPage) -> None:
    if (page.imagelength, page.imagewidth) != array.shape[-2:]:
        raise ValueError(
            f"page shape {(page.imagelength, page.imagewidth)} differs from array shape {array.shape[-2:]}"
        )


def write_page(
    array: zarr.Array, page: tifffile.TiffPage, index: tuple[int, ...]
) -> int:
    """Decode each tile of `page` once and write it at `index`; return the tile count."""
    _check_shape(array, page)
    count = 0
    for channel0, y0, x0, tile in _tiles(page):
        region = _region(page, index, channel0, y0, x0, tile)
        array[region] = tile if page.samplesperpixel > 1 else tile[0]
        count += 1
    return count


def check_page(
    array: zarr.Array,
    page: tifffile.TiffPage,
    index: tuple[int, ...],
    tiles: Sequence[tuple[int, int]],
) -> list[str]:
    """Compare the `(row, col)` tiles of `page` with `array`; return one message per tile that differs."""
    _check_shape(array, page)
    wanted = set(tiles)
    bad = set()
    for channel0, y0, x0, tile in _tiles(page):
        key = (y0 // page.tilelength, x0 // page.tilewidth)
        if key not in wanted:
            continue
        stored = array[_region(page, index, channel0, y0, x0, tile)]
        if not np.array_equal(stored, tile if page.samplesperpixel > 1 else tile[0]):
            bad.add(key)
    return [f"tile {key} differs from the page" for key in sorted(bad)]


def channel_names(ome_xml: str) -> list[str]:
    """Return the `Name` of each `Channel` of the first `Image`, in order."""
    image = next(
        el
        for el in ET.fromstring(ome_xml).iter()
        if el.tag.endswith("}Image") or el.tag == "Image"
    )
    return [
        el.get("Name")
        for el in image.iter()
        if el.tag.endswith("}Channel") or el.tag == "Channel"
    ]
