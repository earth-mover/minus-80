"""The facts of one Atera bundle, read from metadata and small index arrays only.

A sample's record is the facts of its bundle. ``compare`` lists where a bundle differs from the record.
The reader is tolerant. A fact group that fails becomes an ``{"error": ...}`` entry and the other groups stay.
"""

from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import qc
import tifffile
from bundle import (
    CELLS, METRICS_PATTERN, MORPHOLOGY_2D_PATTERN, MORPHOLOGY_3D_PATTERN, REPORT_PATTERN, TRANSCRIPTS, Bundle,
    open_tiff, read_alignment,
)
from minus80.core.pyramid import physical_sizes
from minus80.core.zarr_copy import read_array
from root_attrs import parse_metrics
from sources import S3

RECORDS = Path(__file__).with_name("samples")
DIFFEXP_PREFIX = "diffexp_genes/"
#: The default ceiling of the bytes that one bundle check or survey reads.
DEFAULT_MAX_BYTES = 200_000_000
#: The upper bound of a CSV header line.
HEADER_LIMIT = 1 << 20
#: The longest message that an error entry keeps.
MESSAGE_LIMIT = 500
#: A URL, a name of the source bucket, or an absolute path with all that follows it. A record holds no location.
_LOCATION = re.compile(rf"""\S*://\S*|\S*{re.escape(urlparse(S3).netloc)}\S*|['"(\[]?(?<![\w.~-])/.*""")


class ByteLimit(BaseException):
    """A sample read more than its ceiling. It is no ``Exception``, so ``group`` does not catch it."""


class Ceiling:
    """The progress hook of a read counter. It raises ``ByteLimit`` once the reads pass ``limit`` bytes."""

    def __init__(self, limit: int):
        self.limit, self.total, self.message = limit, 0, None

    def advance(self, size: int) -> None:
        self.total += size
        if self.total > self.limit:
            self.message = f"read {self.total} bytes, over the limit of {self.limit} bytes"
            raise ByteLimit(self.message)


def error_text(error: Exception) -> str:
    """The type of ``error`` and the first line of its message, with every location removed."""
    lines = str(error).splitlines()
    message = _LOCATION.sub("<location>", lines[0])[:MESSAGE_LIMIT] if lines else ""
    return f"{type(error).__name__}: {message}".rstrip()


def group(read: Callable, *args):
    """``read(*args)``, or an error entry when it raises."""
    try:
        return read(*args)
    except Exception as error:
        return {"error": error_text(error)}


def error_paths(record, path: str = "") -> list[str]:
    """The path of each dict in ``record`` that holds an ``error`` entry."""
    if not isinstance(record, dict):
        return []
    found = [path or "."] if isinstance(record.get("error"), str) else []
    for key, value in sorted(record.items()):
        found += error_paths(value, f"{path}.{key}" if path else key)
    return found


def pixel_sizes(page) -> dict[str, float]:
    """The micrometre sizes in the OME-XML of the description of ``page``. A file with no OME-XML gives an empty dict."""
    try:
        return physical_sizes(page.description)
    except (StopIteration, ET.ParseError):
        return {}


def tiff_facts(tif: tifffile.TiffFile) -> dict:
    """The first series of ``tif``: its levels and the layout of the first page of level 0."""
    levels = tif.series[0].levels
    page = levels[0].pages[0]
    compression = page.compression
    return {
        "series": len(tif.series),
        "pages": len(tif.pages),
        "levels": [list(level.shape) for level in levels],
        "planes": len(levels[0].pages),
        "tile": [page.tilelength, page.tilewidth],
        "compression": {"code": int(compression), "name": getattr(compression, "name", str(int(compression)))},
        "planarconfig": int(page.planarconfig),
        "planar": page.planarconfig == tifffile.PLANARCONFIG.SEPARATE,
        "photometric": int(page.photometric),
        "dtype": str(page.dtype),
        "pixel_size": pixel_sizes(tif.pages.first),
    }


def member_tiff_facts(bundle: Bundle, name: str) -> dict:
    with bundle.tiff(name) as tif:
        return tiff_facts(tif)


def he_facts(open_he: Callable) -> dict:
    file = open_he()
    try:
        with open_tiff(file) as tif:
            return tiff_facts(tif)
    finally:
        file.close()


def morphology_facts(bundle: Bundle, pattern: str) -> dict:
    """Every member that matches ``pattern``, with the count. A bad file is an error entry of its own."""
    names = bundle.matching(pattern)
    return {"count": len(names), "files": {n: group(member_tiff_facts, bundle, n) for n in names}}


def metrics_keys(bundle: Bundle, name: str) -> list[str]:
    with bundle.member_file(name) as f:
        return sorted(parse_metrics(f.read().decode()))


def metrics_facts(bundle: Bundle) -> dict:
    names = bundle.matching(METRICS_PATTERN)
    return {"count": len(names), "files": {n: group(metrics_keys, bundle, n) for n in names}}


def experiment_facts(bundle: Bundle) -> dict:
    experiment = bundle.experiment()
    return {"keys": sorted(experiment),
            **{k: experiment[k] for k in ("analysis_sw_version", "pixel_size") if k in experiment}}


def attribute(zf, member: str, key: str):
    """The value of attribute ``key`` in the JSON ``member`` of ``zf``, or an error entry."""
    return group(lambda: json.loads(zf.read(member))[key])


def first_tile(keys, counts) -> str | None:
    """The first tile with rows. The attributes must be the lists that the grid holds."""
    if isinstance(keys, dict) or isinstance(counts, dict):
        raise ValueError("the grid attributes are unreadable")
    return next((k for k, n in zip(keys, counts, strict=True) if n), None)


def first_tile_columns(zf, keys, counts) -> dict:
    """The key of the first tile with rows and the chunk rows of each of its columns."""
    tile = first_tile(keys, counts)
    prefix, suffix = f"grid/{tile}/", "/.zarray"
    # The chunk rows differ between the columns of one tile.
    columns = {n[len(prefix):-len(suffix)]: json.loads(zf.read(n))["chunks"][0]
               for n in zf.namelist() if tile is not None and n.startswith(prefix) and n.endswith(suffix)}
    return {"key": tile, "chunk_rows": columns}


def transcript_facts(bundle: Bundle) -> dict:
    zf = bundle.member_zip(TRANSCRIPTS)
    keys, counts = attribute(zf, "grid/.zattrs", "grid_keys"), attribute(zf, "grid/.zattrs", "grid_number_objects")
    return {"grid_keys": keys, "grid_number_objects": counts, "number_rnas": attribute(zf, ".zattrs", "number_rnas"),
            "first_tile": group(first_tile_columns, zf, keys, counts)}


def polygon_set_facts(zf, names: set[str], key: str, methods) -> dict:
    cell_index = read_array(zf, f"polygon_sets/{key}/cell_index", names=names)
    codes, rows = np.unique(read_array(zf, f"polygon_sets/{key}/method", names=names), return_counts=True)
    known = methods if isinstance(methods, list) else []
    return {
        "rows": len(cell_index),
        "unique_cells": len(np.unique(cell_index)),
        "methods": {f"{int(c)}:{known[c] if 0 <= c < len(known) else '?'}": int(n)
                    for c, n in zip(codes, rows, strict=True)},
    }


def polygon_facts(bundle: Bundle) -> dict:
    """The polygon sets that the store holds. Only ``cell_index`` and ``method`` of each set are read."""
    zf = bundle.member_zip(CELLS)
    names = set(zf.namelist())
    methods = attribute(zf, ".zattrs", "segmentation_methods")
    keys = sorted({m[1] for n in names if (m := re.match(r"polygon_sets/(\d+)/", n))}, key=int)
    return {"set_indices": keys, "method_names": methods,
            "sets": {k: group(polygon_set_facts, zf, names, k, methods) for k in keys}}


def cell_facts(bundle: Bundle) -> dict:
    zf = bundle.member_zip(CELLS)
    return {"count": group(lambda: json.loads(zf.read("cell_id/.zarray"))["shape"][0]),
            "cell_summary_columns": attribute(zf, "cell_summary/.zattrs", "column_names")}


def rasters_facts(rasters: dict) -> dict:
    """The rasters as the ingest sees them: element name, label, dtype, shape, bin size, origin and unit."""
    return {name: {"label": r.label, "dtype": str(r.array.dtype), "shape": list(r.array.shape),
                   "bin_size": r.bin_size, "origin": list(r.origin), "unit": r.unit}
            for name, r in rasters.items()}


def report_facts(bundle: Bundle) -> dict:
    names = bundle.matching(REPORT_PATTERN)
    return {"count": len(names), "files": names}


def qc_facts(bundle: Bundle, decoded: dict | None = None) -> dict:
    """The raw view and the ingest's view of the first report member. The report is read and decoded once.

    The decoded rasters go into ``decoded`` when it is given.
    """
    names = bundle.matching(REPORT_PATTERN)
    if not names:
        raise ValueError(f"no member matches {REPORT_PATTERN}")
    with bundle.member_file(names[0]) as f:
        html = f.read().decode()

    def decode() -> dict:
        found = qc.rasters(html)
        if decoded is not None:
            decoded.update(found)
        return found

    return {"member": names[0], "raw": group(qc.raw, html), "rasters": group(lambda: rasters_facts(decode()))}


def header_facts(bundle: Bundle, name: str) -> dict:
    with bundle.member_file(name) as f:
        line = f.readline(HEADER_LIMIT)
    if len(line) == HEADER_LIMIT and not line.endswith(b"\n"):
        raise ValueError(f"{name}: no line end in the first {HEADER_LIMIT} bytes")
    columns = next(csv.reader([line.decode().rstrip("\r\n")]))
    return {"columns": len(columns), "first": columns[:2]}


def diffexp_facts(bundle: Bundle) -> dict:
    """The column count and first two column names of each ``diffexp_genes`` CSV. Only its header line is read."""
    return {n: group(header_facts, bundle, n) for n in bundle.names
            if n.startswith(DIFFEXP_PREFIX) and n.endswith(".csv")}


def bundle_facts(
    bundle: Bundle, open_he: Callable, alignment_text: Callable, decoded: dict | None = None,
) -> dict:
    """The facts of one bundle, as plain JSON values.

    ``open_he`` returns the open H&E image and ``alignment_text`` the text of the alignment CSV.
    Each is called inside its own group, so a failure on either leaves the other groups in the record.
    The QC group decodes its rasters into ``decoded``.
    The groups run in order, the cheap ones first. When ``ByteLimit`` is raised, the group in progress
    and every later group become error entries, and the earlier groups stay.
    """
    steps = [
        ("members", lambda: dict(sorted(bundle.sizes.items()))),
        ("experiment", lambda: experiment_facts(bundle)),
        ("metrics", lambda: metrics_facts(bundle)),
        ("report", lambda: report_facts(bundle)),
        ("alignment", lambda: read_alignment(alignment_text()).tolist()),
        ("cells", lambda: cell_facts(bundle)),
        ("transcripts", lambda: transcript_facts(bundle)),
        ("diffexp", lambda: diffexp_facts(bundle)),
        ("polygons", lambda: polygon_facts(bundle)),
        ("qc", lambda: qc_facts(bundle, decoded)),
        ("morphology_2d", lambda: morphology_facts(bundle, MORPHOLOGY_2D_PATTERN)),
        ("morphology_3d", lambda: morphology_facts(bundle, MORPHOLOGY_3D_PATTERN)),
        ("he_image", lambda: he_facts(open_he)),
    ]
    found, stopped = {}, None
    for name, read in steps:
        if stopped is None:
            try:
                value = group(read)
            except ByteLimit as limit:
                stopped = f"ByteLimit: {limit}"
        if stopped is not None:
            value = {"error": stopped}
        found[name] = value
    return found


_ABSENT = object()


def _show(value) -> str:
    return "absent" if value is _ABSENT else json.dumps(value, sort_keys=True)


def compare(record: dict, found: dict, path: str = "") -> list[str]:
    """One line ``<path>: record <a>, bundle <b>`` for each differing leaf. A list is one leaf."""
    lines = []
    for key in sorted(record.keys() | found.keys()):
        here = f"{path}.{key}" if path else key
        a, b = record.get(key, _ABSENT), found.get(key, _ABSENT)
        if isinstance(a, dict) and isinstance(b, dict):
            lines += compare(a, b, here)
        elif a != b:
            lines.append(f"{here}: record {_show(a)}, bundle {_show(b)}")
    return lines


def load_record(sample_name: str, directory: Path = RECORDS, *, path: Path | None = None) -> dict:
    """The facts on record for ``sample_name``, without the ``sample`` key.

    The record is the file ``path``. Without ``path``, it is the file of the sample in ``directory``.

    A record with an error entry cannot serve as a reference. ``ValueError`` gives the path of each entry.
    """
    path = path or directory / f"{sample_name}.json"
    if not path.is_file():
        raise ValueError(f"no record of {sample_name}: {path} is absent")
    try:
        record = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise ValueError(f"{path.name} is not JSON") from None
    if record.get("sample") != sample_name:
        raise ValueError(f"{path.name} holds the record of sample {record.get('sample')!r}, not {sample_name!r}")
    del record["sample"]
    if errors := error_paths(record):
        raise ValueError(f"{path.name} holds error entries at {', '.join(errors)}; it cannot serve as a reference")
    return record


def write_record(sample_name: str, found: dict, directory: Path = RECORDS) -> list[str]:
    """Write ``found`` as the record of ``sample_name``, with sorted keys. Returns the path of each error entry."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sample_name}.json"
    path.write_text(json.dumps({"sample": sample_name, **found}, sort_keys=True, indent=2) + "\n")
    return error_paths(found)
