"""Native pyramid levels: TIFF pages decoded once per tile and written as zarr chunks."""

import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor

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


def _crop(
    page: tifffile.TiffPage, segment: np.ndarray, indices: tuple
) -> tuple[int, int, int, np.ndarray]:
    """Return `(channel0, y0, x0, tile)` with the tile cropped to the page, as `(s, h, w)`."""
    sample, _, y0, x0, _ = indices
    tile = segment[0, : page.imagelength - y0, : page.imagewidth - x0]
    return sample, y0, x0, np.moveaxis(tile, -1, 0)


def _tiles(page: tifffile.TiffPage) -> Iterator[tuple[int, int, int, np.ndarray]]:
    """Yield each decoded tile of the page, once."""
    for segment, indices, _ in page.segments():
        if segment is not None:
            yield _crop(page, segment, indices)


def _decode_tile(
    page: tifffile.TiffPage, row: int, col: int
) -> Iterator[tuple[int, int, int, np.ndarray]]:
    """Decode only tile `(row, col)`: one segment, or one per sample on a planar page."""
    per_row = -(-page.imagewidth // page.tilewidth)
    rows = -(-page.imagelength // page.tilelength)
    per_plane = per_row * rows
    # Without this check, a column past the row's end reads the next row's tile.
    if not (0 <= row < rows and 0 <= col < per_row):
        raise IndexError(f"tile ({row}, {col}) is outside the {rows} x {per_row} tile grid of {page}")
    planar = page.planarconfig == tifffile.PLANARCONFIG.SEPARATE
    fh = page.parent.filehandle
    for sample in range(page.samplesperpixel if planar else 1):
        i = sample * per_plane + row * per_row + col
        if not page.databytecounts[i]:
            continue
        with page.parent.filehandle.lock:
            fh.seek(page.dataoffsets[i])
            data = fh.read(page.databytecounts[i])
        segment, indices, _ = page.decode(data, i)
        yield _crop(page, segment, indices)


def _require_tiled(page: tifffile.TiffPage) -> None:
    if not page.is_tiled:
        raise NotImplementedError(f"{page} is striped, not tiled")


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


def _tiles_match_chunks(array: zarr.Array, page: tifffile.TiffPage) -> bool:
    """True when each tile maps to its own chunks, so no two tiles write one chunk."""
    return array.shards is None and tuple(array.chunks[-2:]) == (
        page.tilelength,
        page.tilewidth,
    )


def write_page(
    array: zarr.Array,
    page: tifffile.TiffPage,
    index: tuple[int, ...],
    *,
    concurrency: int = 1,
) -> int:
    """Decode each tile of `page` once and write it at `index`; return the tile count.

    With `concurrency > 1` and tiles that match the chunk grid, up to `concurrency` writes run at once.
    """
    _require_tiled(page)
    _check_shape(array, page)
    writes = (
        (
            _region(page, index, channel0, y0, x0, tile),
            tile if page.samplesperpixel > 1 else tile[0],
        )
        for channel0, y0, x0, tile in _tiles(page)
    )
    count = 0
    if concurrency <= 1 or not _tiles_match_chunks(array, page):
        for region, data in writes:
            array[region] = data
            count += 1
        return count
    # Decoding stays in this thread; the pending cap bounds the tiles held in memory.
    pending: deque = deque()
    with ThreadPoolExecutor(concurrency) as pool:
        for region, data in writes:
            if len(pending) >= 2 * concurrency:
                pending.popleft().result()
            pending.append(pool.submit(array.__setitem__, region, data))
            count += 1
        for future in pending:
            future.result()
    return count


def check_page(
    array: zarr.Array,
    page: tifffile.TiffPage,
    index: tuple[int, ...],
    tiles: Sequence[tuple[int, int]],
) -> list[str]:
    """Compare the `(row, col)` tiles of `page` with `array`; return one message per tile that differs."""
    _require_tiled(page)
    _check_shape(array, page)
    bad = set()
    for row, col in tiles:
        for channel0, y0, x0, tile in _decode_tile(page, row, col):
            stored = array[_region(page, index, channel0, y0, x0, tile)]
            if not np.array_equal(
                stored, tile if page.samplesperpixel > 1 else tile[0]
            ):
                bad.add((row, col))
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
