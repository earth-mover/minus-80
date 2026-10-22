"""Open a TIFF from a URL, a path or a file object, and return one series' levels."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Literal

import tifffile
from virtualizarr.manifests import ManifestArray

from minus80.core import remote
from minus80.core.fetch import download_then_index, sparse_local
from minus80.core.registry import as_url, file_path

from .inspect import directory_layout
from .pages import level_array

Prefetch = Literal["ranges", "directories", "whole"]

#: A BigTIFF header: byte order, version, offset size, and the first directory's offset.
_HEADER = 16


def tiff_levels(
    source: str | Path | BinaryIO,
    *,
    url: str | None = None,
    series: int | str = 0,
    prefetch: Prefetch = "ranges",
    attributes: Mapping | None = None,
) -> dict[str, ManifestArray]:
    """Every resolution level of one series in a TIFF, keyed ``"0"``, ``"1"``, ….

    ``source`` is a URL, a local path, or an open binary file. ``url`` is the
    address the manifest records; it defaults to ``source``'s, and a file
    object has none, so passing one without ``url`` raises ``ValueError``.

    ``series`` is an index into tifffile's series or a series name.

    ``prefetch`` chooses how a remote file's directories are read; a local
    path or a file object ignores it.

    - ``"ranges"`` walks the directories through a block-cached range reader.
      One round trip per directory that is not already cached.
    - ``"directories"`` asks :func:`~minus80.parsers.tiff.inspect.directory_layout`
      where the directories sit, fetches the header and that region into a
      sparse local file, and parses that. It raises ``ValueError`` when the
      layout is not bulk-readable, and when the parse read tag data from
      outside the fetched bytes.
    - ``"whole"`` downloads the file into the cache first.

    Each level is built by :func:`~minus80.parsers.tiff.pages.level_array`;
    ``attributes`` are merged into every level's.
    """
    if prefetch not in ("ranges", "directories", "whole"):
        raise ValueError(f"prefetch {prefetch!r}: expected 'ranges', 'directories' or 'whole'")
    with _opened(source, url, prefetch) as (tif, url, fetched):
        chosen = _series(tif, series)
        if fetched is not None:
            _check_fetched(tif, chosen, fetched)
        return {str(i): level_array(level, url, attributes=attributes)
                for i, level in enumerate(chosen.levels)}


@contextmanager
def _opened(source, url: str | None, prefetch: str) -> Iterator[
        tuple[tifffile.TiffFile, str, list[tuple[int, int]] | None]]:
    """``source`` open in tifffile, the URL to record, and the byte ranges fetched when only some were."""
    fetched = None
    if hasattr(source, "read"):
        if url is None:
            raise ValueError("a file object has no address of its own; pass url=, "
                             "the address the manifest should record")
        handle = source
    elif (path := file_path(source)) is not None:
        handle = path
    elif prefetch == "ranges":
        handle = remote.reader(str(source))
    elif prefetch == "whole":
        handle = download_then_index(str(source))
    else:
        layout = directory_layout(str(source))
        if not layout.bulk_readable:
            raise ValueError(f"{source}: directories are not bulk-readable ({layout.refused}); "
                             f"use prefetch='ranges' or 'whole'")
        fetched = [(0, _HEADER), layout.region]
        handle = sparse_local(str(source), fetched)
    with tifffile.TiffFile(handle) as tif:
        yield tif, url or as_url(source), fetched


def _series(tif: tifffile.TiffFile, series: int | str) -> tifffile.TiffPageSeries:
    if isinstance(series, int):
        return tif.series[series]
    for s in tif.series:
        if s.name == series:
            return s
    raise KeyError(f"no series named {series!r}; the file has {[s.name for s in tif.series]}")


def _check_fetched(tif: tifffile.TiffFile, series: tifffile.TiffPageSeries,
                   fetched: list[tuple[int, int]]) -> None:
    """Raise if parsing a sparse copy read bytes that were never fetched.

    Bytes outside the fetched ranges read as zeros, and tifffile parses zeros
    without complaint: a tile-offset array that lies outside becomes offsets
    and lengths of 0, which the manifest would record as missing chunks. A
    full page's tags record where each value lies, so those are checked
    directly. A frame keeps only its offsets, and no stored tile starts at
    offset 0, where the header is, so a frame whose offsets are all 0 is taken
    as read from a hole. That also refuses a page with no stored tile at all,
    which ``"ranges"`` still reads.
    """
    def inside(start: int, length: int) -> bool:
        return any(a <= start and start + length <= b for a, b in fetched)

    for page in (tif.pages.first, *(level.keyframe for level in series.levels)):
        for tag in page.tags.values():
            if not inside(tag.valueoffset, tag.valuebytecount):
                raise ValueError(f"page {page.index}: tag {tag.name} lies outside the fetched "
                                 f"directories; use prefetch='ranges' or 'whole'")
    for level in series.levels:
        for page in level.pages:
            if page is not None and not any(page.dataoffsets):
                raise ValueError(f"page {page.index}: every tile offset is 0, so its offsets lie "
                                 f"outside the fetched directories; use prefetch='ranges' or 'whole'")
