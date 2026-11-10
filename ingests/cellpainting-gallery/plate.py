"""The pieces ``ingest.py`` builds one Cell Painting Gallery plate from.

Each section below is one step of the pipeline: what a plate's
``load_data.csv`` says, where a field sits and how large its pixels are, the
one pass over each TIFF that indexes it and builds its overview, the
illumination functions, what the image folder holds beyond ``load_data.csv``,
and the attribute documents. Nothing here writes to a store; ``ingest.py``
does, in order.

Fetching and parsing are separate: the functions that read the bucket take a
registry and a URL, and the functions that interpret what was read take the
bytes or the table.
"""

from __future__ import annotations

import io
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import numpy.lib.format as npy_format
import obstore
import pandas as pd
from minus80.core.builders.flat_binary import flat_manifest_array
from minus80.core.fetch import read_range
from minus80.core.manifest import concatenate, manifest_array, stack
from minus80.core.progress import Progress
from minus80.core.registry import resolve
from minus80.ome import channel_omero, image_attrs
from minus80.ome.plate import FORMATS
from minus80.parsers.tiff import TiffParser, lzw_valid_length
from virtualizarr.manifests import ManifestArray
from virtualizarr.manifests.utils import manifest_chunk_shape

import jump

# What load_data.csv says
#
# One row per (well, site), one URL_Orig<channel> column per channel. Every
# image URL comes from here: the image folders also hold thumbnails,
# calibration images and, on source_9, wells this table leaves out, so a
# listing would define a different plate.

URL_PREFIX = "URL_Orig"

#: The c axis order: the five Cell Painting stains as the JUMP protocol lists
#: them, then the brightfield planes in z order (source_2 writes ``_L`` at its
#: first plane and ``_H`` at its third). Fixed rather than
#: sorted, since sorting puts AGP before DNA, and rather than taken from file
#: channel numbers, which differ between sources (LAYOUTS.md).
CHANNELS = ("DNA", "RNA", "ER", "AGP", "Mito", "Brightfield_L", "Brightfield", "Brightfield_H")

#: Display colors: the conventional Cell Painting composite for the stains,
#: gray for brightfield.
COLORS = {"DNA": "0000FF", "RNA": "FFFF00", "ER": "00FF00", "AGP": "FF8000", "Mito": "FF00FF",
          "Brightfield_L": "FFFFFF", "Brightfield": "FFFFFF", "Brightfield_H": "FFFFFF"}

WELL = re.compile(r"([A-Z]{1,2})(\d{1,2})")


@dataclass(frozen=True)
class Field:
    """One (well, site) row of ``load_data.csv``."""

    well: str
    row: str
    column: str
    site: str
    urls: tuple[str, ...]
    record: Mapping[str, str]

    @property
    def path(self) -> str:
        """The field's group path under its plate: row, column and site."""
        return f"{self.row}/{self.column}/{self.site}"


def read_load_data(path) -> pd.DataFrame:
    """``load_data.csv`` as strings, an empty cell as the empty string."""
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def plate_channels(frame: pd.DataFrame) -> list[str]:
    """The channels the plate's ``URL_Orig`` columns name, in :data:`CHANNELS` order.

    A column naming a channel outside :data:`CHANNELS` raises: placing it
    anywhere on the c axis would be a guess.
    """
    named = [c.removeprefix(URL_PREFIX) for c in frame.columns if c.startswith(URL_PREFIX)]
    if unknown := sorted(set(named) - set(CHANNELS)):
        raise ValueError(f"load_data.csv names channels outside the protocol order: {unknown}")
    if not named:
        raise ValueError(f"load_data.csv has no {URL_PREFIX}<channel> columns")
    return [c for c in CHANNELS if c in named]


def parse_well(label: str) -> tuple[str, str]:
    """``"AA16"`` to ``("AA", "16")``, ``"A01"`` to ``("A", "1")``.

    The label is the only reliable statement of a well's place: source_10
    writes ``Metadata_Col`` modulo 10 and source_8 writes ``Metadata_Row`` as
    a letter (LAYOUTS.md).
    """
    if not (m := WELL.fullmatch(label)):
        raise ValueError(f"well label {label!r} is not a row letter or two and a column number")
    return m[1], str(int(m[2]))


def read_fields(frame: pd.DataFrame, channels: Sequence[str]) -> list[Field]:
    """One :class:`Field` per row, in the table's order.

    A site is named as ``Metadata_Site`` gives it, since sources 7 and 13
    count from 0 and every other source from 1 (PLAN decision 8). A repeated
    (well, site) or a site name that is not alphanumeric, which OME-Zarr
    forbids in a field path, raises.
    """
    url_columns = [URL_PREFIX + c for c in channels]
    others = [c for c in frame.columns if not c.startswith(URL_PREFIX)]
    found, seen = [], set()
    for record in frame.to_dict("records"):
        well, site = record["Metadata_Well"], record["Metadata_Site"]
        if not site.isalnum():
            raise ValueError(f"{well}: site {site!r} is not alphanumeric")
        if (well, site) in seen:
            raise ValueError(f"load_data.csv lists {well} site {site} twice")
        seen.add((well, site))
        row, column = parse_well(well)
        urls = tuple(record[c] for c in url_columns)
        if not all(urls):
            raise ValueError(f"{well} site {site}: a channel has no URL")
        found.append(Field(well, row, column, site, urls, {c: record[c] for c in others}))
    return found


def plate_format(fields: Sequence[Field]) -> int:
    """The smallest standard plate, 384 or 1536 wells, that holds every well listed.

    ``load_data.csv`` does not state the format, and neither does JUMP's
    plate table. Sources 1 and 9 image 1536-well plates and list columns past
    24; every other source lists a 384-well grid.
    """
    for wells in (384, 1536):
        rows, columns = FORMATS[wells]
        if all(row_index(f.row) < rows and int(f.column) <= columns for f in fields):
            return wells
    raise ValueError("wells lie outside a 1536-well grid")


def row_index(row: str) -> int:
    """``"A"`` to 0, ``"Z"`` to 25, ``"AA"`` to 26."""
    index = 0
    for letter in row:
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def site_key(site: str) -> tuple[int, str]:
    """Sorts sites by number: ``"10"`` after ``"9"``."""
    return (int(site), site) if site.isdigit() else (1 << 30, site)


def split_columns(frame: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    """The non-URL columns that hold one value over the whole plate, and those that vary.

    Constant columns are stated once on the plate and varying ones on each
    field, so every value the table holds is in the store and none is
    repeated thousands of times.
    """
    others = [c for c in frame.columns if not c.startswith(URL_PREFIX)]
    constant = {c: frame[c].iloc[0] for c in others if frame[c].nunique() == 1}
    return constant, [c for c in others if c not in constant]


# Where a field sits and how large its pixels are

#: load_data.csv states sizes and positions in meters.
METERS_TO_UM = 1e6


def pixel_size(frame: pd.DataFrame, config_um: float) -> tuple[float, str]:
    """The plate's pixel size in micrometers, and where it was stated (PLAN decision 5).

    ``Metadata_ImageResolutionX/Y`` where ``load_data.csv`` states them, else
    the source's ``Metadata_Pixel_Size_Microns`` in JUMP's
    ``microscope_config.csv``. The TIFF tags are never used: they state the
    camera's pixel pitch or a placeholder (LAYOUTS.md). Different values
    across rows, or an x that differs from y, raise.
    """
    columns = ("Metadata_ImageResolutionX", "Metadata_ImageResolutionY")
    if all(c in frame for c in columns):
        values = {float(v) for c in columns for v in frame[c].unique()}
        if len(values) != 1:
            raise ValueError(f"load_data.csv states more than one pixel size: {sorted(values)} m")
        return values.pop() * METERS_TO_UM, ("load_data.csv Metadata_ImageResolutionX/Y, "
                                             "stated in meters")
    return config_um, ("jump-cellpainting/datasets microscope_config.csv "
                       "Metadata_Pixel_Size_Microns for the source; load_data.csv states none")


def has_positions(frame: pd.DataFrame) -> bool:
    return "Metadata_PositionX" in frame and "Metadata_PositionY" in frame


def translation(record: Mapping[str, str], shape: tuple[int, int], pixel_um: float) -> dict:
    """Where the field's first pixel center sits in its well, in micrometers.

    ``Metadata_PositionX/Y`` is the stage position of the field's center
    relative to the well's, in meters. Stage Y grows toward image row 0, the
    opposite of the array's y, so it is negated; the earlier ingest checked
    that sign by edge continuity on Opera Phenix and Operetta plates
    (LAYOUTS.md). A center less half the field, plus half a pixel, is the
    first pixel's center.
    """
    half_y = (shape[0] - 1) * pixel_um / 2
    half_x = (shape[1] - 1) * pixel_um / 2
    return {"c": 0.0,
            "y": -float(record["Metadata_PositionY"]) * METERS_TO_UM - half_y,
            "x": float(record["Metadata_PositionX"]) * METERS_TO_UM - half_x}


def acquisition_times(frame: pd.DataFrame) -> list[datetime] | None:
    """Each row's ``Metadata_AbsTime`` as a time, or None when the column is not every row a timestamp.

    Source_10 writes the constant ``531``, which is no time at all.
    """
    if "Metadata_AbsTime" not in frame:
        return None
    parsed = pd.to_datetime(frame["Metadata_AbsTime"], utc=True, format="ISO8601", errors="coerce")
    return None if parsed.isna().any() else list(parsed)


# One pass over each TIFF
#
# TiffParser indexes the file; the strips' bytes are then fetched once, every
# LZW strip's reference length is checked against the codec, and the decoded
# pixels are reduced to the overview. Nothing is decoded again.

#: The overview's reduction per axis: a field of about 1,000 pixels becomes
#: about 64, the size a plate montage draws a field at.
OVERVIEW_FACTOR = 16


@dataclass
class Indexed:
    """One TIFF after its single pass."""

    url: str
    array: ManifestArray
    tiff: dict
    overview: np.ndarray
    compression: str
    shortened: list[dict] = field(default_factory=list)
    unfixable: list[int] = field(default_factory=list)


def index_and_reduce(url: str, registry, pixel_um: float) -> Indexed:
    """Index one single-page TIFF, check its LZW strips and reduce it to the overview.

    ``TiffParser`` takes the pixel size as an attribute override, so the
    level's ``scale`` is the plate's and the TIFF's resolution tags stay only
    in the group attributes it returns. The strips are read in one range
    request spanning them all.
    """
    parser = TiffParser(attributes={"scale": {"y": pixel_um, "x": pixel_um},
                                    "units": {"y": "micrometer", "x": "micrometer"}})
    group = parser(url, registry).group
    if list(group.arrays) != ["0"] or group.arrays["0"].ndim != 2:
        raise ValueError(f"{url}: expected one (y, x) level, got "
                         f"{ {k: a.shape for k, a in group.arrays.items()} }")
    array = group.arrays["0"]
    codecs = [c if isinstance(c, dict) else c.to_dict() for c in array.metadata.to_dict()["codecs"]]
    names = [c["name"] for c in codecs]
    lzw = "imagecodecs_lzw" in names
    if names not in (["bytes"], ["bytes", "imagecodecs_lzw"]):
        raise NotImplementedError(f"{url}: codecs {names}")
    endian = "<" if codecs[0]["configuration"]["endian"] == "little" else ">"

    rows, width = manifest_chunk_shape(array.metadata)
    grid = array.manifest.shape_chunk_grid
    entries = [array.manifest.get_entry((i, 0)) for i in range(grid[0])]
    entries = [None if e is None else {"offset": int(e["offset"]), "length": int(e["length"])}
               for e in entries]
    if any(e is None for e in entries):
        raise ValueError(f"{url}: a strip has no stored bytes")
    start = min(e["offset"] for e in entries)
    end = max(e["offset"] + e["length"] for e in entries)
    data = read_range(url, start, end, registry=registry)

    nbytes = rows * width * array.dtype.itemsize
    lengths, shortened, unfixable, strips = [], [], [], []
    for i, entry in enumerate(entries):
        strip = data[entry["offset"] - start: entry["offset"] - start + entry["length"]]
        if lzw:
            length, decoded = lzw_valid_length(strip, nbytes)
            if length is None:
                unfixable.append(i)
                length = entry["length"]
            elif length < entry["length"]:
                shortened.append({"strip": i, "stored_bytes": entry["length"],
                                  "referenced_bytes": length})
        else:
            length, decoded = entry["length"], strip
        if len(decoded) != nbytes:
            raise ValueError(f"{url}: strip {i} decodes to {len(decoded)} bytes, expected {nbytes}")
        lengths.append(length)
        strips.append(decoded)
    plane = np.frombuffer(b"".join(strips), dtype=array.dtype.newbyteorder(endian)).reshape(array.shape)
    if shortened:
        array = with_lengths(array, codecs, np.array(lengths, dtype=np.uint64).reshape(grid))
    return Indexed(url=url, array=array, tiff=dict(group.metadata.attributes),
                   overview=block_mean(plane, OVERVIEW_FACTOR),
                   compression="LZW" if lzw else "none", shortened=shortened, unfixable=unfixable)


def with_lengths(array: ManifestArray, codecs: list[dict], lengths: np.ndarray) -> ManifestArray:
    """``array`` with each chunk's reference length replaced by ``lengths``, everything else kept."""
    grid = array.manifest.shape_chunk_grid
    keys = list(np.ndindex(*grid))
    entries = [array.manifest.get_entry(k) for k in keys]
    paths = np.array([e["path"] for e in entries], dtype=object).reshape(grid)
    offsets = np.array([e["offset"] for e in entries], dtype=np.uint64).reshape(grid)
    attributes = {k: v for k, v in (array.metadata.attributes or {}).items() if k != "source_url"}
    return manifest_array(paths, offsets, lengths, shape=array.shape, chunk_shape=manifest_chunk_shape(array.metadata),
                          dtype=array.dtype, codecs=codecs,
                          dimension_names=array.metadata.dimension_names, attributes=attributes)


def block_mean(plane: np.ndarray, factor: int) -> np.ndarray:
    """Each ``factor`` x ``factor`` block's mean, rounded to the plane's dtype.

    A mean rather than a subsample: at 16x a nucleus is a pixel or two, and
    subsampling would drop whole cells. Blocks at the bottom and right edges
    that the plane does not fill average the pixels they hold, so the
    overview covers every pixel of the field.
    """
    height, width = plane.shape
    ys, xs = np.arange(0, height, factor), np.arange(0, width, factor)
    sums = np.add.reduceat(np.add.reduceat(plane.astype(np.float64), ys, axis=0), xs, axis=1)
    counts = np.outer(np.diff(np.append(ys, height)), np.diff(np.append(xs, width)))
    return np.rint(sums / counts).astype(plane.dtype.newbyteorder("="))


def index_all(urls: Sequence[str], registry, pixel_um: float, workers: int) -> dict[str, Indexed]:
    """:func:`index_and_reduce` on every URL, concurrently; raises after the run if any failed."""
    done, failed = {}, {}
    with Progress("indexing and reducing TIFFs", len(urls), unit="files") as progress, \
            ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(index_and_reduce, u, registry, pixel_um): u for u in urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                done[url] = future.result()
            except Exception as exc:  # noqa: BLE001 -- collected and raised below
                failed[url] = f"{type(exc).__name__}: {exc}"
            progress.advance()
    if failed:
        for url, why in sorted(failed.items())[:10]:
            print(f"  failed {url}: {why}", file=sys.stderr)
        raise RuntimeError(f"{len(failed)} of {len(urls)} TIFFs failed; first: "
                           f"{next(iter(sorted(failed.items())))}")
    return done


def lzw_records(f: Field, parts: Sequence[Indexed], channels: Sequence[str]) -> tuple[list, list]:
    """The field's shortened and unfixable LZW chunks, located by field path and (c, strip, 0)."""
    shortened, unfixable = [], []
    for c, part in enumerate(parts):
        for s in part.shortened:
            shortened.append({"field": f.path, "channel": channels[c], "chunk": [c, s["strip"], 0],
                              "stored_bytes": s["stored_bytes"],
                              "referenced_bytes": s["referenced_bytes"]})
        for strip in part.unfixable:
            unfixable.append({"field": f.path, "channel": channels[c], "chunk": [c, strip, 0],
                              "url": part.url})
    return shortened, unfixable


# A field image

#: The overview's display window: the plate's darkest and brightest 0.1% of
#: overview pixels are clipped, so a few saturated pixels do not flatten the
#: rest. One window per channel for the whole plate keeps wells comparable.
WINDOW_PERCENTILES = (0.1, 99.9)


def field_level0(indexed: Sequence[Indexed], channels: Sequence[str],
                 translation_um: Mapping[str, float] | None) -> ManifestArray:
    """The field's channels stacked into one (c, y, x) level 0, one chunk per strip.

    ``minus80.core.manifest.stack`` names the new axis and keeps the
    channels' shared attributes, and raises if the channels' TIFFs disagree
    on shape, dtype, codec or pixel size.
    """
    array = stack([i.array for i in indexed], dim="c", axis={"type": "channel"}, position=0)
    attrs = array.metadata.attributes
    attrs["channel_names"] = list(channels)
    if translation_um is not None:
        attrs["translation"] = {"y": translation_um["y"], "x": translation_um["x"]}
    return array


def display_windows(overviews: Sequence[np.ndarray], channels: Sequence[str]) -> list[dict]:
    """One ``omero`` channel entry per channel, its window from every overview of the plate."""
    stacked = np.stack(overviews)
    out = []
    for c, name in enumerate(channels):
        start, end = np.percentile(stacked[:, c], WINDOW_PERCENTILES)
        out.append({"label": name, "color": COLORS[name], "start": round(float(start)),
                    "end": round(float(end))})
    return out


def field_ome(*, name: str, level0: ManifestArray, overview_shape: tuple[int, ...],
              pixel_um: float, windows: Sequence[Mapping],
              translation_um: Mapping[str, float] | None) -> dict:
    """The field's ``ome`` document: level 0 referenced, level 1 the written overview.

    ``minus80.ome.image_attrs`` derives level 1's pixel size from the ratio
    of the two shapes. Where the field is not a multiple of 16 pixels the
    last block averages fewer pixels, and the derived size differs from 16
    pixels by up to 0.7% (about half an overview pixel at the far edge).
    """
    return image_attrs(
        name=name, dims=("c", "y", "x"), level_shapes=[level0.shape, overview_shape],
        scale={"c": 1.0, "y": pixel_um, "x": pixel_um},
        units={"y": "micrometer", "x": "micrometer"},
        dimensions=level0.metadata.attributes["dimensions"],
        downsampling="mean", translation=translation_um,
        description=(f"level 0 references the source TIFFs, one chunk per strip; level 1 is a "
                     f"{OVERVIEW_FACTOR}x{OVERVIEW_FACTOR} block mean of level 0, zstd-compressed"),
        omero=channel_omero(windows, max_value=65535))


# Illumination functions
#
# CellProfiler wrote one .npy per plate and channel: a NumPy header, then the
# array in C order. The header states the dtype, shape and where the data
# starts; numpy.lib.format reads it, so no offset is assumed.

#: Bytes fetched for a header. A NumPy 1.0 header is 128 bytes as numpy
#: writes it; a longer one is fetched again at its stated length.
HEADER_FETCH = 4096


@dataclass(frozen=True)
class NpyHeader:
    url: str
    dtype: np.dtype
    shape: tuple[int, ...]
    data_offset: int
    size: int


def list_objects(registry, folder_url: str) -> dict[str, int]:
    """Every object with bytes under ``folder_url``, by URL, with its size."""
    store, key = resolve(registry, folder_url + "x")
    prefix = key[:-1]
    root = folder_url[: len(folder_url) - len(prefix)]
    found = {}
    for batch in obstore.list(store, prefix=prefix):
        for obj in batch:
            if obj["size"]:
                found[root + obj["path"]] = int(obj["size"])
    return found


def read_npy_header(url: str, size: int, registry) -> NpyHeader:
    """The header of the ``.npy`` at ``url``, checked against the object's size."""
    head = read_range(url, 0, min(size, HEADER_FETCH), registry=registry)
    handle = io.BytesIO(head)
    version = npy_format.read_magic(handle)
    length_bytes = 2 if version == (1, 0) else 4
    header_end = 8 + length_bytes + int.from_bytes(head[8:8 + length_bytes], "little")
    if header_end > len(head):
        handle = io.BytesIO(read_range(url, 0, header_end, registry=registry))
        npy_format.read_magic(handle)
    reader = npy_format.read_array_header_1_0 if version == (1, 0) else npy_format.read_array_header_2_0
    shape, fortran_order, dtype = reader(handle)
    if fortran_order:
        raise NotImplementedError(f"{url}: Fortran-ordered array")
    offset = handle.tell()
    expected = offset + int(np.prod(shape)) * dtype.itemsize
    if expected != size:
        raise ValueError(f"{url}: header says {expected} bytes, the object holds {size}")
    return NpyHeader(url, dtype, tuple(int(s) for s in shape), offset, size)


def illum_array(headers: Sequence[NpyHeader], channels: Sequence[str],
                pixel_um: float) -> ManifestArray:
    """The plate's illumination functions as one (c, y, x) array, one chunk per file.

    Each file becomes a (1, y, x) array from its own data offset
    (``flat_manifest_array``), and the channels are joined along c. The files
    must agree on dtype and shape, which come from their headers.
    """
    if len({(h.dtype, h.shape) for h in headers}) != 1:
        raise ValueError("illumination files disagree on dtype or shape: "
                         f"{[(h.url.rsplit('/', 1)[-1], str(h.dtype), h.shape) for h in headers]}")
    attributes = {"dimensions": {"c": {"type": "channel"}, "y": {"type": "space"},
                                 "x": {"type": "space"}},
                  "scale": {"y": pixel_um, "x": pixel_um},
                  "units": {"y": "micrometer", "x": "micrometer"}}
    parts = []
    for h in headers:
        if len(h.shape) != 2:
            raise ValueError(f"{h.url}: shape {h.shape}, expected (y, x)")
        endian = "big" if h.dtype.byteorder == ">" else "little"
        parts.append(flat_manifest_array(
            h.url, shape=(1, *h.shape), dtype=h.dtype.newbyteorder("="), data_offset=h.data_offset,
            endian=endian, dimension_names=("c", "y", "x"), attributes=attributes))
    array = concatenate(parts, dim="c")
    array.metadata.attributes["channel_names"] = list(channels)
    return array


def illum_ome(*, name: str, array: ManifestArray, pixel_um: float) -> dict:
    """The illumination image's ``ome`` document.

    No ``omero`` block: a display window for these floating-point gain maps
    would need their values, and reading them is not needed to reference them.
    """
    return image_attrs(name=name, dims=("c", "y", "x"), level_shapes=[array.shape],
                       scale={"c": 1.0, "y": pixel_um, "x": pixel_um},
                       units={"y": "micrometer", "x": "micrometer"},
                       dimensions=array.metadata.attributes["dimensions"],
                       description="one level, each channel's .npy referenced from its data offset")


# What the image folder holds beyond load_data.csv

#: File name patterns that state a well, per microscope family (LAYOUTS.md).
#: Opera and Operetta name rows by number.
NAMED_WELL = (re.compile(r"r(?P<row>\d{2})c(?P<column>\d{2})f\d{2}p\d{2}-ch\d+"),
              re.compile(r"_(?P<well>[A-Z]{1,2}\d{2})_T\d{4}F\d{3}"),
              re.compile(r"^HTS_(?P<well>[A-Z]{1,2}\d{2})_s\d+_w"))


def well_in_name(name: str) -> str | None:
    for pattern in NAMED_WELL:
        if m := pattern.search(name):
            if "well" in m.groupdict():
                return m["well"]
            row = int(m["row"]) - 1
            letters = chr(ord("A") + row) if row < 26 else "A" + chr(ord("A") + row - 26)
            return f"{letters}{int(m['column']):02d}"
    return None


def folder_extras(listed: Sequence[str], referenced: set[str], wells: set[str]) -> dict:
    """What the image folders hold that ``load_data.csv`` does not reference (PLAN decision 9).

    Files whose name states a well are counted per well when the well is not
    in ``load_data.csv`` and in one count when it is (extra sites, source_8's
    thumbnails). Files naming no well (instrument and calibration files) are
    counted with a few examples.
    """
    unlisted_wells: Counter = Counter()
    listed_wells = []
    other = []
    for url in listed:
        if url in referenced:
            continue
        name = url.rsplit("/", 1)[-1]
        well = well_in_name(name)
        if well is None:
            other.append(name)
        elif well in wells:
            listed_wells.append(name)
        else:
            unlisted_wells[well] += 1
    return {"objects_listed": len(listed),
            "wells_not_in_load_data": dict(sorted(unlisted_wells.items())),
            "unreferenced_files_of_listed_wells": {"count": len(listed_wells),
                                                   "examples": sorted(listed_wells)[:3]},
            "files_naming_no_well": {"count": len(other), "examples": sorted(other)[:5]}}



# The attribute documents
#
# What the plate and each well say about themselves beyond OME-Zarr's own
# fields, written so that a reader of the store needs nothing else.

#: The sources whose TIFFs hold 256 strips each, so a field is 1,280 chunks
#: and a reader with zarr's default concurrency waits on them in turn.
MANY_STRIPS = {"source_8"}


def well_fields(well: str, perturbations: dict, plate_listed: bool) -> dict:
    """A well's annotations: its label and JUMP's perturbation, or why there is none."""
    out = {"well": well}
    if well in perturbations:
        return out | perturbations[well]
    out["perturbation"] = ("not recorded: the plate is not in JUMP's plate.csv.gz" if not plate_listed
                           else "not recorded: well.csv.gz has no row for this well")
    return out


def plate_fields(*, source, batch, name, load_data_url, every_field, fields, subset,
                 wells_format, channels, plate_row, coverage, pixel_um, pixel_source,
                 positions, times, constant, varying, compression, shortened, unfixable,
                 extras, illum_path) -> dict:
    """The plate's annotations: what the plate is and every judgement the ingest made on it."""
    sites = sorted({f.site for f in every_field}, key=site_key)
    not_recorded = [
        what for what, stated in (("field positions", positions), ("acquisition times", times))
        if not stated]
    out = {
        "description": (f"Plate {name} of batch {batch}, imaged by {source} for JUMP Cell "
                        f"Painting: an OME-Zarr HCS plate, one field image per (well, site) "
                        f"that load_data.csv lists."),
        "source": source, "batch": batch, "plate": name,
        "load_data_url": load_data_url,
        "plate_format": {"wells": wells_format,
                         "rule": "the smallest standard plate (384 or 1536 wells) that holds "
                                 "every well load_data.csv lists; neither it nor JUMP's "
                                 "plate.csv.gz states the format"},
        "channels": channels,
        "load_data": {
            "fields": len(every_field), "wells": len({f.well for f in every_field}),
            "sites": sites,
            "columns_constant": constant,
            "columns_per_field": varying,
            "note": "columns_constant holds every non-URL column with one value over the whole "
                    "table; the others are on each field's annotations.load_data",
        },
        "ingested": ("every row of load_data.csv" if not subset else
                     {"fields": len(fields), "wells": sorted({f.well for f in fields}),
                      "sites": sorted({f.site for f in fields}, key=site_key),
                      "note": "a subset of load_data.csv, chosen when the plate was ingested"}),
        "jump_plate_table": ({"listed": True, "plate_type": plate_row["Metadata_PlateType"]}
                             if plate_row else
                             {"listed": False,
                              "note": f"plate.csv.gz at {jump.COMMIT} has no row for "
                                      f"({source}, {batch}, {name}), so the plate has no plate "
                                      f"type and its wells no perturbation"}),
        "perturbation_join": coverage | {
            "keys": "well.csv.gz by (Metadata_Source, Metadata_Plate, Metadata_Well); "
                    "compound, orf, crispr and perturbation_control by Metadata_JCP2022",
            "commit": jump.COMMIT},
        "pixel_size_um": pixel_um,
        "pixel_size_source": pixel_source,
        "positions": ("each field's translation is load_data.csv's Metadata_PositionX/Y (the "
                      "field center, in meters, relative to the well center), with Y negated"
                      if positions else "not recorded in load_data.csv"),
        "acquisition": ("ome.plate.acquisitions[0] spans load_data.csv's Metadata_AbsTime, "
                        "starttime and endtime in seconds since the Unix epoch"
                        if times else "not recorded in load_data.csv"),
        "level0_compression": compression,
        "lzw_shortened_chunks": {
            "description": (
                "Level-0 chunks whose reference stops 1 to 4 bytes before the end of the TIFF "
                "strip: the strip's end code is written at the previous code width, so the "
                "Zarr LZW codec, which decodes until an end code, would read past the image. "
                "Each shortened reference was checked to decode byte-identical to the strip's "
                "decode bounded at the image's size, as tifffile reads it."),
            "chunks": shortened},
        "lzw_unfixable_chunks": {
            "description": ("Level-0 chunks no cut of 1 to 4 bytes lets the Zarr LZW codec "
                            "decode; their references are kept whole and fail to read."),
            "chunks": unfixable},
        "image_folder": {
            "folders": sorted({u.rsplit('/', 1)[0] + '/' for f in every_field for u in f.urls}),
            "beyond_load_data": extras,
            "note": "the store follows load_data.csv (PLAN decision 9); these files are in the "
                    "folders and not in it"},
        "illumination": illum_path or "not written: a channel has no illumination file",
    }
    if not_recorded:
        out["not_recorded"] = {
            "fields": not_recorded,
            "description": ("what load_data.csv does not state for this plate. The instrument's "
                            "files beside the images may (LAYOUTS.md), and are not read."),
        }
    if source in MANY_STRIPS:
        out["readers"] = ("each TIFF holds 256 strips of 4 rows, so a field's level 0 is "
                          f"{len(channels) * 256} chunks; raise zarr's async.concurrency (128 "
                          "reads a field in under a second, the default 10 in about 8 s)")
    return out
