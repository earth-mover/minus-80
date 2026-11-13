"""Native pyramid levels: TIFF pages decoded once per tile and written as zarr chunks."""

import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import tifffile
import zarr
from zarr.codecs import BloscCodec

# Upper bound on the bytes of writes queued at once; one write always proceeds.
PENDING_BYTES = 256 << 20
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


def _read_tile(page: tifffile.TiffPage, row: int, col: int) -> list[tuple[int, bytes]]:
    """Read the stored bytes of tile `(row, col)` as `(segment index, bytes)`: one, or one per sample if planar."""
    per_row = -(-page.imagewidth // page.tilewidth)
    rows = -(-page.imagelength // page.tilelength)
    per_plane = per_row * rows
    # Without this check, a column past the row's end reads the next row's tile.
    if not (0 <= row < rows and 0 <= col < per_row):
        raise IndexError(f"tile ({row}, {col}) is outside the {rows} x {per_row} tile grid of {page}")
    planar = page.planarconfig == tifffile.PLANARCONFIG.SEPARATE
    fh = page.parent.filehandle
    segments = []
    for sample in range(page.samplesperpixel if planar else 1):
        i = sample * per_plane + row * per_row + col
        if not page.databytecounts[i]:
            continue
        with fh.lock:
            fh.seek(page.dataoffsets[i])
            segments.append((i, fh.read(page.databytecounts[i])))
    return segments


def _decode_segments(
    page: tifffile.TiffPage, segments: Sequence[tuple[int, bytes]]
) -> Iterator[tuple[int, int, int, np.ndarray]]:
    """Decode each `(segment index, bytes)` of `page`; this holds no lock and reads no file."""
    for i, data in segments:
        segment, indices, _ = page.decode(data, i)
        yield _crop(page, segment, indices)


def _decode_tile(
    page: tifffile.TiffPage, row: int, col: int
) -> Iterator[tuple[int, int, int, np.ndarray]]:
    """Decode only tile `(row, col)`: one segment, or one per sample on a planar page."""
    yield from _decode_segments(page, _read_tile(page, row, col))


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
    return (
        array.shards is None
        and all(length == 1 for length in array.chunks[:-2])
        and tuple(array.chunks[-2:]) == (page.tilelength, page.tilewidth)
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
    The pending tiles stay under `PENDING_BYTES`, unless one tile exceeds it.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")
    _require_tiled(page)
    _check_shape(array, page)
    writes = (
        (
            _region(page, index, channel0, y0, x0, tile),
            tile if page.samplesperpixel > 1 else tile[0],
        )
        for channel0, y0, x0, tile in _tiles(page)
    )
    tile_bytes = page.tilelength * page.tilewidth * page.samplesperpixel * array.dtype.itemsize
    return _write_all(
        array,
        writes,
        concurrency if _tiles_match_chunks(array, page) else 1,
        tile_bytes,
    )


def _write_all(array: zarr.Array, writes, concurrency: int, write_bytes: int) -> int:
    """Write each `(region, data)` pair of at most `write_bytes`; return the count.

    Up to `concurrency` writes run at once.
    The pending writes stay under `PENDING_BYTES`, unless one write exceeds it.
    """
    count = 0
    if concurrency <= 1:
        for region, data in writes:
            array[region] = data
            count += 1
        return count
    # The decode stays in this thread; the pending cap bounds the tiles held in memory.
    limit = max(1, min(2 * concurrency, PENDING_BYTES // write_bytes))
    pending: deque = deque()
    pool = ThreadPoolExecutor(concurrency)
    try:
        for region, data in writes:
            if len(pending) >= limit:
                pending.popleft().result()
            pending.append(pool.submit(array.__setitem__, region, data))
            count += 1
        for future in pending:
            future.result()
    finally:
        # On an error, the pool drops queued writes, and running ones finish before the error propagates.
        pool.shutdown(cancel_futures=True)
    return count


def _check_stack(
    array: zarr.Array, pages: Sequence[tifffile.TiffPage], index: tuple[int, ...]
) -> tifffile.TiffPage:
    """Raise `ValueError` unless `pages` fill the plane axis and each tile position is one whole shard."""
    if not pages:
        raise ValueError("no pages to write")
    first = pages[0]
    for page in pages:
        _require_tiled(page)
        _check_shape(array, page)
        if page.samplesperpixel != 1:
            raise NotImplementedError(f"{page} has more than one sample per pixel")
        if (page.tilelength, page.tilewidth) != (first.tilelength, first.tilewidth):
            raise ValueError(f"tile size of {page} differs from {first}")
    if len(pages) != array.shape[-3]:
        raise ValueError(f"{len(pages)} pages for {array.shape[-3]} planes")
    if len(index) != array.ndim - 3:
        raise ValueError(f"index {index} must have {array.ndim - 3} entries")
    shard = tuple(array.shards or array.chunks)
    if any(length != 1 for length in shard[:-3]):
        raise ValueError(f"shard shape {shard} must have length 1 before the last three axes")
    unit = shard[-3:]
    if unit != (len(pages), first.tilelength, first.tilewidth):
        raise ValueError(
            f"shard shape {unit} is not (planes, tile height, tile width)"
            f" = {(len(pages), first.tilelength, first.tilewidth)}"
        )
    return first


def write_stack(
    array: zarr.Array,
    pages: Sequence[tifffile.TiffPage],
    index: tuple[int, ...],
    *,
    concurrency: int = 1,
    decoders: int = 1,
) -> int:
    """Write one plane per page at `index`, one region per tile position; return the region count.

    With `decoders > 1`, the planes of one position decode in that many threads.
    The calling thread reads the tile bytes, so no thread seeks the shared file handle.

    An interior region is one whole shard, so the codec writes it once, not once per plane.
    A cropped edge region is a partial shard, so zarr reads that shard once before it writes.
    A tile with no stored bytes reads as the fill value.
    The pending regions stay under `PENDING_BYTES`, unless one region exceeds it.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")
    if decoders < 1:
        raise ValueError(f"decoders must be at least 1, got {decoders}")
    first = _check_stack(array, pages, index)
    height, width = array.shape[-2:]
    tile_h, tile_w = first.tilelength, first.tilewidth

    def fill(stack, z, page, segments):
        for _, _, _, tile in _decode_segments(page, segments):
            stack[z] = tile[0]

    def writes(pool):
        for y0 in range(0, height, tile_h):
            for x0 in range(0, width, tile_w):
                stack = np.full(
                    (len(pages), min(tile_h, height - y0), min(tile_w, width - x0)),
                    array.fill_value,
                    array.dtype,
                )
                row, col = y0 // tile_h, x0 // tile_w
                # The caller reads every tile first: tifffile loads tag values lazily without the lock.
                segments = [_read_tile(page, row, col) for page in pages]
                if pool is None:
                    for z, page in enumerate(pages):
                        fill(stack, z, page, segments[z])
                else:
                    # Each thread fills its own plane; `result` re-raises the first failure.
                    for future in [pool.submit(fill, stack, z, page, segments[z]) for z, page in enumerate(pages)]:
                        future.result()
                region = index + (
                    slice(None),
                    slice(y0, y0 + stack.shape[1]),
                    slice(x0, x0 + stack.shape[2]),
                )
                yield region, stack

    region_bytes = len(pages) * tile_h * tile_w * array.dtype.itemsize
    pool = ThreadPoolExecutor(decoders, thread_name_prefix="stack-decode") if decoders > 1 else None
    if pool is not None:
        # tifffile builds a page's decoder on first access, which is not thread-safe.
        for page in pages:
            page.decode  # noqa: B018
    try:
        return _write_all(array, writes(pool), concurrency, region_bytes)
    finally:
        # A failed decode drops the queued planes; running ones finish before the error propagates.
        if pool is not None:
            pool.shutdown(cancel_futures=True)


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


_MICROMETRE = {"µm", "μm", "um", "micrometer"}


def physical_sizes(ome_xml: str) -> dict[str, float]:
    """Return the micrometre `PhysicalSize` of each axis that the first `Image` states, keyed `x`, `y`, `z`."""
    image = next(
        el
        for el in ET.fromstring(ome_xml).iter()
        if el.tag.endswith("}Image") or el.tag == "Image"
    )
    pixels = next(el for el in image.iter() if el.tag.endswith("}Pixels") or el.tag == "Pixels")
    sizes = {}
    for axis in "XYZ":
        if (value := pixels.get(f"PhysicalSize{axis}")) is None:
            continue
        if (unit := pixels.get(f"PhysicalSize{axis}Unit", "µm")) not in _MICROMETRE:
            raise ValueError(f"PhysicalSize{axis} is in {unit}, not micrometres")
        sizes[axis.lower()] = float(value)
    return sizes
