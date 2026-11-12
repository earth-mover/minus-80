"""The Atera tables that annotate no SpatialData element: the codewords and the differential expression."""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter

import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv
from minus80.core.zarr_copy import read_array

from transcripts import CATEGORY_COLUMNS


def _required(table: pa.Table, index_column: str) -> pa.Table:
    """``table`` with ``index_column`` non-nullable; a null in it, or no such column, raises ``ValueError``."""
    if index_column not in table.column_names:
        raise ValueError(f"the table has no column {index_column!r}")
    fields = [f.with_nullable(False) if f.name == index_column else f for f in table.schema]
    return table.cast(pa.schema(fields))


def codewords_table(zf: zipfile.ZipFile) -> pa.Table:
    """One row per codeword; names and feature indices come from the root attributes."""
    attrs = json.loads(zf.read(".zattrs"))
    feature_names = pa.array(attrs["codeword_gene_names"], type=pa.string())
    feature_index = np.asarray(attrs["codeword_gene_mapping"], dtype=np.int32)
    category = read_array(zf, "codeword_category")
    columns = {
        "codeword_index": pa.array(np.arange(len(feature_index), dtype=np.int64)),
        "feature_index": pa.array(feature_index),
        "feature_name": feature_names,
    }
    columns.update({n: pa.array(category[:, i]) for i, n in enumerate(CATEGORY_COLUMNS)})
    return _required(pa.table(columns), "codeword_index")


def diffexp_table(text: str) -> pa.Table:
    """A ``diffexp_genes`` CSV with its own column names and the types that Arrow infers; ``Feature ID`` is the index.

    A column with no value is float64, because Iceberg has no null type. A repeated column name raises ``ValueError``.
    """
    table = pacsv.read_csv(io.BytesIO(text.encode()))
    if repeated := [name for name, count in Counter(table.column_names).items() if count > 1]:
        raise ValueError(f"the CSV has the column {repeated[0]!r} more than once")
    typed = pa.schema([pa.field(f.name, pa.float64()) if pa.types.is_null(f.type) else f for f in table.schema])
    return _required(table.cast(typed), "Feature ID")
