"""One transcript tile of an Atera ``transcripts.zarr.zip`` as an Arrow table."""

from __future__ import annotations

import json
import zipfile

import numpy as np
import pyarrow as pa
from bundle import read_v2_array

CATEGORY_COLUMNS = (
    "is_gene",
    "is_negative_control_codeword",
    "is_negative_control_probe",
    "is_unassigned_codeword",
    "is_predesigned_gene",
    "is_deprecated_codeword",
    "is_custom_gene",
    "is_genomic_control",
    "is_boosted_gene",
)

_BBOX = pa.struct([(n, pa.float64()) for n in ("xmin", "ymin", "xmax", "ymax")])

TRANSCRIPT_SCHEMA = pa.schema(
    [
        ("instance", pa.int64()),
        ("grid_tile", pa.string()),
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("z", pa.float32()),
        ("codeword_index", pa.uint32()),
        ("feature_name", pa.dictionary(pa.int32(), pa.string())),
        ("cell_prefix", pa.uint32()),
        ("dataset_suffix", pa.uint32()),
        ("cell_id", pa.string()),
        ("quality_score", pa.float32()),
        ("overlaps_nucleus", pa.bool_()),
        ("bbox", _BBOX),
    ]
)

# The table has no geometry column: ``bbox`` is the point and the covering declares it.
GEO_POINTS = {
    "version": "1.1.0",
    "primary_column": "geometry",
    "columns": {
        "geometry": {
            "encoding": "WKB",
            "geometry_types": ["Point"],
            "covering": {
                "bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}
            },
        }
    },
}


def xenium_cell_id(prefix: np.ndarray, suffix: np.ndarray) -> np.ndarray:
    """Xenium cell ids: the prefix as 8 base-26 letters, a is 0, then ``-`` and the suffix."""
    digits = np.empty((len(prefix), 8), dtype="U1")
    rest = prefix.astype(np.uint64)
    for place in range(7, -1, -1):
        digits[:, place] = (rest % 26 + ord("a")).astype(np.uint8).view("S1").astype("U1")
        rest //= 26
    letters = digits.view("U8").ravel()
    return np.char.add(np.char.add(letters, "-"), suffix.astype(str))


def codeword_table(zf: zipfile.ZipFile) -> pa.Table:
    """One row per codeword; ``feature_names`` and ``codeword_feature_index`` come from the root attributes."""
    attrs = json.loads(zf.read(".zattrs"))
    feature_names = np.array(attrs["feature_names"], dtype=object)
    feature_index = np.asarray(attrs["codeword_feature_index"], dtype=np.int32)
    category = read_v2_array(zf, "codeword_category")
    columns = {
        "codeword_index": pa.array(np.arange(len(feature_index), dtype=np.uint32)),
        "feature_index": pa.array(feature_index),
        "feature_name": pa.array(feature_names[feature_index], type=pa.string()),
    }
    columns.update({n: pa.array(category[:, i]) for i, n in enumerate(CATEGORY_COLUMNS)})
    return pa.table(columns)


def tile_table(
    zf: zipfile.ZipFile, tile: str, codewords: pa.Table, *, first_instance: int
) -> pa.Table:
    """The transcripts of ``grid/<tile>``, numbered from ``first_instance``."""
    read = lambda name: read_v2_array(zf, f"grid/{tile}/{name}")
    location = read("location")
    codeword = read("codeword_identity")[:, 0]
    cell = read("cell_id")
    n = len(location)
    x, y, z = location.T
    names = codewords["feature_name"].take(pa.array(codeword)).dictionary_encode()
    return pa.table(
        {
            "instance": pa.array(np.arange(first_instance, first_instance + n, dtype=np.int64)),
            "grid_tile": pa.array([tile] * n, type=pa.string()),
            "x": x,
            "y": y,
            "z": z,
            "codeword_index": codeword,
            "feature_name": names,
            "cell_prefix": cell[:, 0],
            "dataset_suffix": cell[:, 1],
            "cell_id": pa.array(xenium_cell_id(cell[:, 0], cell[:, 1]), type=pa.string()),
            "quality_score": read("quality_score")[:, 0].astype(np.float32),
            "overlaps_nucleus": read("overlaps_nucleus")[:, 0].astype(bool),
            "bbox": pa.StructArray.from_arrays(
                [pa.array(v.astype(np.float64)) for v in (x, y, x, y)],
                fields=list(_BBOX),
            ),
        },
        schema=TRANSCRIPT_SCHEMA,
    ).combine_chunks()
