"""Questions to ask a TIFF, by URL or local path, before indexing it.

:func:`directory_layout`
    Where a BigTIFF keeps its image file directories, and whether they are a
    contiguous array that can be fetched in one read. Walking the chain costs
    one round trip per directory; fetching the array costs one. Neither the
    file's size nor its name says which applies.
:func:`codec_chain`
    What codecs a page needs, and whether any of them is outside Zarr's v3
    core. A store with only core codecs opens in any Zarr reader; one with
    others needs the package that registers them.
:func:`indexing_cost`
    The time to walk the directories over range reads, sampled and
    extrapolated, beside the size of the metadata region a bulk fetch would
    read. Which of the two is faster depends on bandwidth and latency, so on
    where you run.
:func:`survey`
    A directory listing, from an HTTP index or a local directory, with each
    file's size, magic bytes and TIFF flavour.

Each returns data rather than printing, so it can be used in a notebook, and
each has a subcommand in the command-line interface below::

    python -m minus80.parsers.tiff.inspect layout s3://bucket/image.ome.tif
"""

from __future__ import annotations

import dataclasses
import re
import struct
import time
import urllib.request
from typing import Any

import tifffile
from obspec_utils.registry import ObjectStoreRegistry

from minus80.core import remote
from minus80.core.fetch import object_size, read_range
from minus80.core.registry import file_path

from .pages import page_codecs

#: Codecs Zarr v3 defines itself. Anything else needs a package to register it,
#: which a reader in another language may simply not have.
CORE_CODECS = frozenset({
    "bytes", "transpose", "blosc", "gzip", "zstd", "crc32c",
    "sharding_indexed",
})


@dataclasses.dataclass(frozen=True)
class DirectoryLayout:
    """Where a TIFF's directories sit, and whether they can be fetched in one read."""

    size: int
    first_directory: int
    bigtiff: bool
    stride: int | None = None
    entries: int | None = None
    directories: int | None = None
    #: Why a bulk read was refused, or ``None`` when it was not.
    refused: str | None = None

    @property
    def position(self) -> float:
        """How far into the file the first directory sits, as a fraction."""
        return self.first_directory / self.size if self.size else 0.0

    @property
    def bulk_readable(self) -> bool:
        return self.refused is None and self.directories is not None

    @property
    def region(self) -> tuple[int, int] | None:
        """The byte range holding every directory, when there is one."""
        return (self.first_directory, self.size) if self.bulk_readable else None


def directory_layout(source: str, *, minimum: int = 64, padding: int = 64,
                     registry: ObjectStoreRegistry | None = None) -> DirectoryLayout:
    """Where this TIFF keeps its directories, and whether they are an array.

    Reads the file size, the 16-byte header, and the entry counts and next
    pointers of the first two directories, without walking the chain. The
    spacing of the second and third directories is taken as the stride. If the
    file from the second directory on is a whole number of strides, and a
    stride exceeds one directory's size by at most ``padding`` bytes, the
    directories are treated as an array that one read of
    :attr:`DirectoryLayout.region` fetches. Fewer than ``minimum``
    directories are reported as not worth a bulk read. Only BigTIFF is judged.
    Reads go through ``registry`` when one is given. docs/upstream.md says
    where this belongs upstream.
    """
    size = object_size(source, patient=True, registry=registry)

    def read(at: int, length: int) -> bytes:
        return read_range(source, at, at + length, patient=True, registry=registry)

    head = read(0, 16)
    if head[:2] not in (b"II", b"MM"):
        return DirectoryLayout(size, 0, False, refused="not a TIFF")
    endian = "<" if head[:2] == b"II" else ">"
    (version,) = struct.unpack_from(endian + "H", head, 2)
    if version != 43:
        (first,) = struct.unpack_from(endian + "I", head, 4)
        return DirectoryLayout(size, first, False,
                               refused="classic TIFF, capped at 4 GB")

    (first,) = struct.unpack_from(endian + "Q", head, 8)
    entries = _entries(read, first, endian)
    second = _next(read, first, endian)
    third = _next(read, second, endian) if second else None
    if not second or not third:
        return DirectoryLayout(size, first, True, entries=entries,
                               refused="fewer than three directories")

    stride = third - second
    entries = _entries(read, second, endian) or entries
    common = {"size": size, "first_directory": first, "bigtiff": True,
              "stride": stride, "entries": entries}
    if stride <= 0 or entries is None:
        return DirectoryLayout(**common, refused="unreadable second directory")
    if (size - second) % stride:
        return DirectoryLayout(**common,
                               refused="directories are not evenly spaced, so the "
                                       "tail is not a uniform array")
    if stride - (8 + entries * 20 + 8) > padding:
        return DirectoryLayout(**common,
                               refused="directories are evenly spaced but not "
                                       "adjacent, so something sits between them")
    count = (size - second) // stride + 1
    if count < minimum:
        return DirectoryLayout(**common, directories=count,
                               refused=f"only {count} directories; a clean division "
                                       f"over so few is chance, and walking them is cheap")
    return DirectoryLayout(**common, directories=count)


def codec_chain(source: str) -> dict[str, Any]:
    """What decoding the first page needs, and whether a plain Zarr reader has it.

    The chain is :func:`~minus80.parsers.tiff.pages.page_codecs`', the one a
    store written from this file would carry. ``portable`` is the question that
    matters: a store whose codecs are all in Zarr's v3 core opens with ``zarr``
    and nothing else, in any language. One outside it means every reader needs
    the package that registers it. A page the parser refuses is reported with
    ``supported`` false and the reason.
    """
    with tifffile.TiffFile(remote.reader(source)) as tif:
        page = tif.pages.first
        compression = tifffile.COMPRESSION(page.compression).name
        try:
            codecs = page_codecs(page)
        except NotImplementedError as exc:
            return {"compression": compression, "supported": False, "reason": str(exc)}
    names = [c["name"] for c in codecs]
    outside = sorted(set(names) - CORE_CODECS)
    return {"compression": compression, "supported": True, "codecs": names,
            "outside_core": outside, "portable": not outside}


def indexing_cost(source: str, *, pages: int = 200) -> dict[str, Any]:
    """What indexing this file costs, both ways, measured rather than guessed.

    Times a sample of the directory walk over byte ranges and reports what the
    whole file would cost at that rate, against what fetching the metadata
    region and parsing it locally would cost. The two invert depending on
    bandwidth and latency, so neither is the answer in general.
    """
    layout = directory_layout(source)
    started = time.perf_counter()
    with tifffile.TiffFile(remote.reader(source)) as tif:
        tif.pages.useframes = True
        tif.pages.set_keyframe(0)
        walked = 0
        for i in range(pages):
            try:
                frame = tif.pages[i]
            except (IndexError, TypeError):
                break
            if frame is None:
                break
            # Touched, not stored: reading the attribute is what forces the
            # round trip, and the round trip is what is being timed.
            _ = frame.dataoffsets
            walked += 1
    per_page = (time.perf_counter() - started) / max(walked, 1)

    out = {
        "file_bytes": layout.size,
        "first_directory_at": f"{layout.position:.2%}",
        "sampled_pages": walked,
        "seconds_per_page_over_ranges": round(per_page, 5),
        "bulk_readable": layout.bulk_readable,
        "refused": layout.refused,
    }
    if layout.directories:
        out["directories"] = layout.directories
        out["walk_seconds"] = round(per_page * layout.directories, 1)
    if layout.region:
        start, end = layout.region
        out["region_bytes"] = end - start
        out["region_share_of_file"] = f"{(end - start) / layout.size:.2%}"
    return out


#: Files in an Apache-style directory index.
_HREF = re.compile(r'href="([^"?/][^"]*)"')


def survey(prefix: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """A directory listing with a header summary per file.

    The first thing wanted when handed an archive prefix and no documentation:
    what is here, how big, and what shape. One `HEAD` and a 16-byte read per
    file, so it stays cheap on a directory of very large files.
    """
    names = _listing(prefix)

    found = []
    for name in names[:limit]:
        url = prefix + name
        row: dict[str, Any] = {"name": name}
        try:
            row["bytes"] = object_size(url, patient=True)
            head = read_range(url, 0, 16, patient=True)
            row["magic"] = head[:4].hex()
            if head[:2] in (b"II", b"MM"):
                endian = "<" if head[:2] == b"II" else ">"
                (version,) = struct.unpack_from(endian + "H", head, 2)
                row["format"] = "BigTIFF" if version == 43 else "TIFF"
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop a survey
            row["error"] = f"{type(exc).__name__}: {exc}"[:80]
        found.append(row)
    return found


def _listing(prefix: str) -> list[str]:
    """The file names under a prefix, from an HTTP index or a local directory.

    An archive serves an Apache index, but a staging directory on disk is the
    same question asked of somewhere else, and refusing it would send the
    caller back to writing their own loop.
    """
    if prefix.startswith(("http://", "https://")):
        with urllib.request.urlopen(prefix, timeout=120) as response:
            body = response.read().decode("utf-8", "replace")
        return sorted(set(_HREF.findall(body)))
    local = file_path(prefix)
    return sorted(p.name for p in local.iterdir() if p.is_file())


def _entries(read, at: int, endian: str) -> int | None:
    (count,) = struct.unpack_from(endian + "Q", read(at, 8), 0)
    return count if 0 < count < 1000 else None


def _next(read, at: int, endian: str) -> int | None:
    count = _entries(read, at, endian)
    if count is None:
        return None
    (nxt,) = struct.unpack_from(endian + "Q", read(at + 8 + count * 20, 8), 0)
    return nxt or None


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="question", required=True)
    for name, help_text in (
        ("layout", "where a TIFF keeps its directories, and whether they bulk-read"),
        ("codecs", "what decoding a page needs, and whether a plain reader has it"),
        ("cost", "what indexing costs, range-reading against fetching"),
    ):
        one = sub.add_parser(name, help=help_text)
        one.add_argument("source")
    listing = sub.add_parser("survey", help="a directory listing with header summaries")
    listing.add_argument("prefix")
    listing.add_argument("--limit", type=int, default=20)

    args = ap.parse_args(argv)
    if args.question == "layout":
        print(json.dumps(dataclasses.asdict(directory_layout(args.source)), indent=2))
    elif args.question == "codecs":
        print(json.dumps(codec_chain(args.source), indent=2))
    elif args.question == "cost":
        print(json.dumps(indexing_cost(args.source), indent=2))
    else:
        print(json.dumps(survey(args.prefix, limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
