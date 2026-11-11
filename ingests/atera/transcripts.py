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

# Iceberg has no unsigned type, so the source's uint32 columns are int64 here.
TRANSCRIPT_SCHEMA = pa.schema(
    [
        ("instance", pa.int64()),
        ("grid_tile", pa.string()),
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("z", pa.float32()),
        ("codeword_index", pa.int64()),
        ("feature_name", pa.dictionary(pa.int32(), pa.string())),
        ("cell_prefix", pa.int64()),
        ("dataset_suffix", pa.int64()),
        ("cell_id", pa.string()),
        ("quality_score", pa.float32()),
        ("overlaps_nucleus", pa.bool_()),
        ("bbox", _BBOX),
    ]
)

def xenium_cell_id(prefix: np.ndarray, suffix: np.ndarray) -> np.ndarray:
    """Xenium cell ids: the prefix as 8 hex nibbles, 0 to 15 as a to p, most significant first, then ``-`` and the suffix."""
    shifts = np.arange(28, -4, -4, dtype=np.uint32)
    nibbles = ((prefix.astype(np.uint32)[:, None] >> shifts) & 0xF).astype(np.uint8) + ord("a")
    letters = np.ascontiguousarray(nibbles).view("S8").ravel().astype("U8")
    return np.char.add(np.char.add(letters, "-"), suffix.astype(str))


def codeword_table(zf: zipfile.ZipFile) -> pa.Table:
    """One row per codeword; names and feature indices come from the root attributes."""
    attrs = json.loads(zf.read(".zattrs"))
    feature_names = pa.array(attrs["codeword_gene_names"], type=pa.string())
    feature_index = np.asarray(attrs["codeword_gene_mapping"], dtype=np.int32)
    category = read_v2_array(zf, "codeword_category")
    columns = {
        "codeword_index": pa.array(np.arange(len(feature_index), dtype=np.int64)),
        "feature_index": pa.array(feature_index),
        "feature_name": feature_names,
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
            "codeword_index": codeword.astype(np.int64),
            "feature_name": names,
            "cell_prefix": cell[:, 0].astype(np.int64),
            "dataset_suffix": cell[:, 1].astype(np.int64),
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
