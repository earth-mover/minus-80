"""The facts of one Atera bundle, read from metadata and small index arrays only.

A sample's record is the facts of its bundle; ``compare`` lists where a bundle differs from the record.
"""

from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import tifffile
from bundle import CELLS, TRANSCRIPTS, Bundle, open_tiff, read_alignment
from minus80.core.pyramid import physical_sizes
from minus80.core.zarr_copy import read_array
from qc import bundle_rasters
from root_attrs import parse_metrics

RECORDS = Path(__file__).with_name("samples")
DIFFEXP_PREFIX = "diffexp_genes/"
#: The upper bound of a CSV header line.
HEADER_LIMIT = 1 << 20


def pixel_sizes(page) -> dict[str, float]:
    """The micrometre sizes in the OME-XML of ``page``'s description; empty when the file has no OME-XML."""
    try:
        return physical_sizes(page.description)
    except (StopIteration, ET.ParseError):
        return {}


def tiff_facts(tif: tifffile.TiffFile) -> dict:
    levels = tif.series[0].levels
    page = levels[0].pages[0]
    return {
        "levels": [list(level.shape) for level in levels],
        "tile": [page.tilelength, page.tilewidth],
        "compression": page.compression.name,
        "planar": page.planarconfig == tifffile.PLANARCONFIG.SEPARATE,
        "dtype": str(page.dtype),
        "pixel_size": pixel_sizes(tif.pages.first),
    }


def member_tiff_facts(bundle: Bundle, name: str) -> dict:
    with bundle.tiff(name) as tif:
        return {"member": name, **tiff_facts(tif), "planes": len(tif.series[0].levels[0].pages)}


def transcript_facts(bundle: Bundle) -> dict:
    zf = bundle.member_zip(TRANSCRIPTS)
    grid = json.loads(zf.read("grid/.zattrs"))
    tile = next((k for k, n in zip(grid["grid_keys"], grid["grid_number_objects"], strict=True) if n), None)
    prefix, suffix = f"grid/{tile}/", "/.zarray"
    # The chunk rows differ between the columns of one tile.
    chunk_rows = {
        name[len(prefix):-len(suffix)]: json.loads(zf.read(name))["chunks"][0]
        for name in zf.namelist() if tile is not None and name.startswith(prefix) and name.endswith(suffix)
    }
    return {
        "grid_keys": grid["grid_keys"],
        "grid_number_objects": grid["grid_number_objects"],
        "number_rnas": json.loads(zf.read(".zattrs"))["number_rnas"],
        "chunk_rows_tile": tile,
        "chunk_rows": chunk_rows,
    }


def polygon_facts(bundle: Bundle) -> dict:
    """Rows, unique cells and rows per method name of each polygon set; only ``cell_index`` and ``method`` are read."""
    zf = bundle.member_zip(CELLS)
    names = set(zf.namelist())
    methods = json.loads(zf.read(".zattrs"))["segmentation_methods"]
    found = {}
    keys = [m[1] for n in names if (m := re.fullmatch(r"polygon_sets/(\d+)/cell_index/\.zarray", n))]
    for key in sorted(keys, key=int):
        cell_index = read_array(zf, f"polygon_sets/{key}/cell_index", names=names)
        codes, rows = np.unique(read_array(zf, f"polygon_sets/{key}/method", names=names), return_counts=True)
        if len(unknown := [int(c) for c in codes if c >= len(methods)]):
            raise ValueError(f"polygon set {key} has the method codes {unknown}; the methods are {methods}")
        found[key] = {
            "rows": len(cell_index),
            "unique_cells": len(np.unique(cell_index)),
            "methods": {methods[int(c)]: int(n) for c, n in zip(codes, rows, strict=True)},
        }
    return found


def cell_facts(bundle: Bundle) -> dict:
    zf = bundle.member_zip(CELLS)
    return {
        "count": json.loads(zf.read("cell_id/.zarray"))["shape"][0],
        "cell_summary_columns": json.loads(zf.read("cell_summary/.zattrs"))["column_names"],
    }


def qc_facts(bundle: Bundle) -> dict:
    return {
        name: {"label": r.label, "dtype": str(r.array.dtype), "shape": list(r.array.shape), "bin_size": r.bin_size,
               "origin": list(r.origin), "unit": r.unit}
        for name, r in bundle_rasters(bundle).items()
    }


def diffexp_facts(bundle: Bundle) -> dict:
    """The column count and first two column names of each ``diffexp_genes`` CSV; only its header line is read."""
    found = {}
    for name in bundle.names:
        if name.startswith(DIFFEXP_PREFIX) and name.endswith(".csv"):
            with bundle.member_file(name) as f:
                line = f.readline(HEADER_LIMIT)
            if len(line) == HEADER_LIMIT and not line.endswith(b"\n"):
                raise ValueError(f"{name}: no line end in the first {HEADER_LIMIT} bytes")
            columns = next(csv.reader([line.decode().rstrip("\r\n")]))
            found[name] = {"columns": len(columns), "first": columns[:2]}
    return found


def bundle_facts(bundle: Bundle, he_file, alignment_text: str) -> dict:
    """The facts of one bundle, as plain JSON values. ``he_file`` is the open H&E image."""
    experiment = bundle.experiment()
    with bundle.member_file(bundle.metrics()) as f:
        metrics = parse_metrics(f.read().decode())
    with open_tiff(he_file) as he:
        he_image = tiff_facts(he)
    stack = member_tiff_facts(bundle, bundle.morphology_3d())
    planes = {name: member_tiff_facts(bundle, name) for name in bundle.morphology_2d()}
    return {
        "members": dict(sorted(bundle.sizes.items())),
        "experiment": {"keys": sorted(experiment), "analysis_sw_version": experiment.get("analysis_sw_version"),
                       "pixel_size": experiment.get("pixel_size")},
        "metrics": {"member": bundle.metrics(), "keys": sorted(metrics)},
        "morphology_2d": {name: {k: v for k, v in entry.items() if k not in ("member", "planes")}
                          for name, entry in planes.items()},
        "morphology_3d": stack,
        "he_image": he_image,
        "transcripts": transcript_facts(bundle),
        "polygons": polygon_facts(bundle),
        "cells": cell_facts(bundle),
        "qc": qc_facts(bundle),
        "diffexp": diffexp_facts(bundle),
        "alignment": read_alignment(alignment_text).tolist(),
    }


def _show(value) -> str:
    return "absent" if value is _ABSENT else json.dumps(value, sort_keys=True)


_ABSENT = object()


def compare(record: dict, found: dict, path: str = "") -> list[str]:
    """One line ``<path>: record <a>, bundle <b>`` for each differing leaf; a list is one leaf."""
    lines = []
    for key in sorted(record.keys() | found.keys()):
        here = f"{path}.{key}" if path else key
        a, b = record.get(key, _ABSENT), found.get(key, _ABSENT)
        if isinstance(a, dict) and isinstance(b, dict):
            lines += compare(a, b, here)
        elif a != b:
            lines.append(f"{here}: record {_show(a)}, bundle {_show(b)}")
    return lines


def load_record(sample_name: str, directory: Path = RECORDS) -> dict:
    """The facts on record for ``sample_name``, without the ``sample`` key."""
    path = directory / f"{sample_name}.json"
    if not path.is_file():
        raise ValueError(f"no record of {sample_name}: {path} is absent")
    record = json.loads(path.read_text())
    if record.get("sample") != sample_name:
        raise ValueError(f"{path.name} holds the record of sample {record.get('sample')!r}, not {sample_name!r}")
    del record["sample"]
    return record


def write_record(sample_name: str, found: dict, directory: Path = RECORDS) -> Path:
    """Write ``found`` as the record of ``sample_name``, with sorted keys."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sample_name}.json"
    path.write_text(json.dumps({"sample": sample_name, **found}, sort_keys=True, indent=2) + "\n")
    return path
