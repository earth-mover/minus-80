"""Questions to ask a remote TIFF before indexing it.

:func:`directory_layout`
    Where a TIFF keeps its image file directories, and whether they are a
    contiguous array that can be fetched in one read. Walking the chain costs
    one round trip per directory; fetching the array costs one. Neither the
    file's size nor its name says which applies.
:func:`codec_chain`
    What decoders a page needs, and whether any of them is outside Zarr's v3
    core. A store nobody can open without a format-specific import is a
    different artifact from one that opens anywhere, and you want to know which
    you are about to build.
:func:`indexing_cost`
    Round trips, bytes moved and parse time against fetch time. Indexing cost
    is round trips times chain length, not bytes, and the two strategies --
    range-read the metadata, or fetch the file and read it locally -- invert
    depending on where you run.
:func:`survey`
    A directory listing with a header summary per file, which is what you want
    first when handed an archive prefix and no documentation.

Each returns data rather than printing, so it can be used in a notebook, and
each has a subcommand in the command-line interface below::

    python -m minus80.parsers.tiff.inspect layout s3://bucket/image.ome.tif
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import struct
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import obstore

from minus80.core.remote import patient_store

#: Codecs Zarr v3 defines itself. Anything else needs a package to register it,
#: which a reader in another language may simply not have.
CORE_CODECS = frozenset({
    "bytes", "endian", "transpose", "blosc", "gzip", "zstd", "crc32c",
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


def directory_layout(url: str, *, minimum: int = 64, padding: int = 64) -> DirectoryLayout:
    """Where this TIFF keeps its directories, and whether they are an array.

    Four requests and no walk: the file size, the 16-byte header, and
    directories one and two for the stride. A writer that emits every plane's
    pixels first and every directory afterwards leaves the directories
    contiguous and equally sized. Then::

        (end of file - second directory offset) % stride == 0

    holds only for a uniform array, and the quotient is how many directories
    there are. The stride comes from the second gap, not the first, because the
    first directory often carries the OME-XML and is larger than the rest.

    Even spacing does not prove adjacency: a writer that emits each page as
    directory-then-pixels also has a constant stride. So a stride more than
    ``padding`` bytes beyond one directory's size is refused, and so is a file
    with fewer than ``minimum`` directories, where a clean division is chance
    and walking the chain is cheap anyway.
    """
    store, key, _ = patient_store(url)
    size = obstore.head(store, key)["size"]
    head = _read(store, key, 0, 16)
    if head[:2] not in (b"II", b"MM"):
        return DirectoryLayout(size, 0, False, refused="not a TIFF")
    endian = "<" if head[:2] == b"II" else ">"
    (version,) = struct.unpack_from(endian + "H", head, 2)
    if version != 43:
        (first,) = struct.unpack_from(endian + "I", head, 4)
        return DirectoryLayout(size, first, False,
                               refused="classic TIFF, capped at 4 GB")

    (first,) = struct.unpack_from(endian + "Q", head, 8)
    entries = _entries(store, key, first, endian)
    second = _next(store, key, first, endian)
    third = _next(store, key, second, endian) if second else None
    if not second or not third:
        return DirectoryLayout(size, first, True, entries=entries,
                               refused="fewer than three directories")

    stride = third - second
    entries = _entries(store, key, second, endian) or entries
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


def codec_chain(url: str) -> dict[str, Any]:
    """What decoding one page needs, and whether a plain Zarr reader has it.

    ``portable`` is the question that matters: a store whose codecs are all in
    Zarr's v3 core opens with ``zarr`` and nothing else, in any language. One
    outside it means every reader needs the package that registers it.
    """
    import tifffile

    from minus80.core.remote import reader as remote_reader

    with tifffile.TiffFile(remote_reader(url)) as tif:
        page = tif.pages[0]
        names = ["bytes"]
        compression = int(page.compression)
        if compression != 1:
            names.append(f"imagecodecs_{tifffile.COMPRESSION(compression).name.lower()}")
        if int(page.planarconfig) == 1 and int(page.samplesperpixel) > 1:
            names.append("transpose")
        outside = sorted(n for n in names if n not in CORE_CODECS)
        return {
            "compression": tifffile.COMPRESSION(compression).name,
            "samples_per_pixel": int(page.samplesperpixel),
            "interleaved": int(page.planarconfig) == 1 and int(page.samplesperpixel) > 1,
            "codecs": names,
            "outside_core": outside,
            "portable": not outside,
        }


def indexing_cost(url: str, *, pages: int = 200) -> dict[str, Any]:
    """What indexing this file costs, both ways, measured rather than guessed.

    Times a sample of the directory walk over byte ranges and reports what the
    whole file would cost at that rate, against what fetching the metadata
    region and parsing it locally would cost. The two invert depending on
    bandwidth and latency, so neither is the answer in general.
    """
    import tifffile

    from minus80.core.remote import reader as remote_reader

    layout = directory_layout(url)
    started = time.perf_counter()
    with tifffile.TiffFile(remote_reader(url)) as tif:
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
            store, key, _ = patient_store(url)
            row["bytes"] = obstore.head(store, key)["size"]
            head = _read(store, key, 0, 16)
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
    local = pathlib.Path(
        url2pathname(urlparse(prefix).path) if prefix.startswith("file://") else prefix)
    return sorted(p.name for p in local.iterdir() if p.is_file())


def _read(store, key, start: int, length: int) -> bytes:
    return bytes(memoryview(
        obstore.get_range(store, key, start=start, end=start + length).to_bytes()))


def _entries(store, key, at: int, endian: str) -> int | None:
    (count,) = struct.unpack_from(endian + "Q", _read(store, key, at, 8), 0)
    return count if 0 < count < 1000 else None


def _next(store, key, at: int, endian: str) -> int | None:
    count = _entries(store, key, at, endian)
    if count is None:
        return None
    (nxt,) = struct.unpack_from(
        endian + "Q", _read(store, key, at + 8 + count * 20, 8), 0)
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
        one.add_argument("url")
    listing = sub.add_parser("survey", help="a directory listing with header summaries")
    listing.add_argument("prefix")
    listing.add_argument("--limit", type=int, default=20)

    args = ap.parse_args(argv)
    if args.question == "layout":
        print(json.dumps(dataclasses.asdict(directory_layout(args.url)), indent=2))
    elif args.question == "codecs":
        print(json.dumps(codec_chain(args.url), indent=2))
    elif args.question == "cost":
        print(json.dumps(indexing_cost(args.url), indent=2))
    else:
        print(json.dumps(survey(args.prefix, limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
