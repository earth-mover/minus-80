"""The QC rasters in the data bag of an analysis summary."""

from __future__ import annotations

import base64
import json
import math
import re
import zlib
from dataclasses import dataclass

import numpy as np

#: The text that opens the data bag. An earlier occurrence sits in an HTML comment.
BAG_TAG = '<script id="ws-data" type="application/json">'
RESOURCE_PREFIX = "_resources_"
DTYPES = {"uint16": "<u2", "float32": "<f4"}
#: The element name of each label in ``density_plot["bins"]``. A label that this table lacks raises.
BIN_NAMES = {
    "Genomic Controls": "genomic_controls",
    "Mean Q-Score": "mean_q_score",
    "Negative Control Codewords": "negative_control_codewords",
    "Negative Control Probes": "negative_control_probes",
    "Transcript Density Map": "transcript_density",
}
#: The bag gives the coarse map as a key, with no label or bin count; its bin size is in the key.
COARSE_KEY = "mean_q_score_80um"
COARSE_NAME, COARSE_LABEL, COARSE_BIN = "mean_q_score_80um", "Mean Q-Score 80 µm", 80.0
#: The element names in the order of PLAN.md, "The tree".
QC_NAMES = ("transcript_density", "mean_q_score", COARSE_NAME, "negative_control_codewords",
            "negative_control_probes", "genomic_controls")
_DATA_URI = re.compile(r"data:([^/,]*)/zip;base64,(.*)", re.DOTALL)
_SPACE = re.compile(r"\s*")


@dataclass(frozen=True)
class QcRaster:
    """A ``(rows = y, columns = x)`` map; ``origin`` is ``(x, y)`` at the outer corner of bin 0."""

    name: str
    label: str
    array: np.ndarray
    bin_size: float
    origin: tuple[float, float]
    unit: str


def data_bag(html: str) -> dict:
    """The JSON object after the last :data:`BAG_TAG`."""
    at = html.rfind(BAG_TAG)
    if at < 0:
        raise ValueError(f"the report has no {BAG_TAG}")
    start = _SPACE.match(html, at + len(BAG_TAG)).end()
    return json.JSONDecoder().raw_decode(html, start)[0]


def decode(uri: str, shape: tuple[int, int]) -> np.ndarray:
    """The raster in the data URI ``uri``; its value count must fill ``shape``."""
    match = _DATA_URI.fullmatch(uri)
    header = uri.split(",", 1)[0]
    if not match or match[1] not in DTYPES:
        raise ValueError(f"cannot decode a raster with the header {header}; known: {', '.join(DTYPES)}")
    values = np.frombuffer(zlib.decompress(base64.b64decode(match[2])), dtype=DTYPES[match[1]])
    if values.size != shape[0] * shape[1]:
        raise ValueError(f"{header}: {values.size} values do not fill {shape[0]} rows by {shape[1]} columns")
    return values.reshape(shape)


def rasters(html: str) -> dict[str, QcRaster]:
    """Every QC raster of the report, by element name."""
    bag = data_bag(html)
    plot = bag["density_plot"]
    width, height = plot["dimensions"]["width"], plot["dimensions"]["height"]
    origin, unit = tuple(plot["origin_data"]), plot["unit"]
    fine = width / plot["horizontal_bin_count"]
    found = {}
    maps = [(label, BIN_NAMES.get(label), key, fine) for label, key in plot["bins"].items()]
    maps.append((COARSE_LABEL, COARSE_NAME, plot[COARSE_KEY], COARSE_BIN))
    for label, name, key, bin_size in maps:
        if name is None:
            raise ValueError(f"unknown QC raster label {label!r}; known: {', '.join(BIN_NAMES)}")
        shape = (math.ceil(height / bin_size), math.ceil(width / bin_size))
        array = decode(bag["_resources"][key.removeprefix(RESOURCE_PREFIX)], shape)
        found[name] = QcRaster(name, label, array, bin_size, origin, unit)
    return found


def bundle_rasters(bundle) -> dict[str, QcRaster]:
    """The rasters of the bundle's one ``analysis_summary*.html`` member."""
    reports = [n for n in bundle.names if n.startswith("analysis_summary") and n.endswith(".html")]
    if len(reports) != 1:
        raise ValueError(f"expected one analysis_summary*.html member, found {len(reports)}: {reports}")
    with bundle.member_file(reports[0]) as f:
        return rasters(f.read().decode())
