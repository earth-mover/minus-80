"""One transcript tile of an Atera ``transcripts.zarr.zip`` as an Arrow table."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from minus80.core.tables import parquet_schema
from minus80.core.zarr_copy import read_rows
from polygons import BBOX

#: Rows of a tile held in memory at once. A tile has up to tens of millions of rows.
SLICE_ROWS = 4_000_000

_COLUMN_ARRAYS = ("location", "codeword_identity", "cell_id", "quality_score", "overlaps_nucleus")
_NIBBLE_LETTERS = pa.array([chr(ord("a") + i) for i in range(16)])

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

# Iceberg has no unsigned type, so the source's uint32 columns are int64 here.
TRANSCRIPT_SCHEMA = pa.schema(
    [
        pa.field("instance", pa.int64(), nullable=False),
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
        ("bbox", BBOX),
    ]
)

def xenium_cell_id(prefix: np.ndarray, suffix: np.ndarray) -> np.ndarray:
    """Xenium cell ids: the prefix as 8 hex nibbles, 0 to 15 as a to p, most significant first, then ``-`` and the suffix."""
    shifts = np.arange(28, -4, -4, dtype=np.uint32)
    nibbles = ((prefix.astype(np.uint32)[:, None] >> shifts) & 0xF).astype(np.uint8) + ord("a")
    letters = np.ascontiguousarray(nibbles).view("S8").ravel().astype("U8")
    return np.char.add(np.char.add(letters, "-"), suffix.astype(str))


def cell_ids_arrow(prefix: pa.Array, suffix: pa.Array) -> pa.Array:
    """The same ids as :func:`xenium_cell_id`, from integer arrays, with Arrow compute only."""
    letters = [
        pc.take(_NIBBLE_LETTERS, pc.bit_wise_and(pc.shift_right(prefix, shift), 15))
        for shift in range(28, -4, -4)
    ]
    return pc.binary_join_element_wise(
        pc.binary_join_element_wise(*letters, ""), pc.cast(suffix, pa.string()), "-"
    )


def tile_slices(
    zf: zipfile.ZipFile,
    tile: str,
    codewords: pa.Table,
    *,
    first_instance: int,
    rows: int = SLICE_ROWS,
) -> Iterator[pa.Table]:
    """The transcripts of ``grid/<tile>`` in tables of at most ``rows`` rows, numbered from ``first_instance``.

    Each slice reads only the source chunks that its rows touch. A slice holds whole chunks of the column with the widest chunks.
    A chunk above ``rows`` rows raises ``ValueError``.
    """
    members = set(zf.namelist())
    total = json.loads(zf.read(f"grid/{tile}/location/.zarray"))["shape"][0]
    # One dictionary for every slice, so the files and the table agree on the codes.
    features = codewords["feature_name"].combine_chunks().dictionary_encode()
    chunk_rows = {n: json.loads(zf.read(f"grid/{tile}/{n}/.zarray"))["chunks"][0] for n in _COLUMN_ARRAYS}
    widest = max(chunk_rows, key=chunk_rows.get)
    if chunk_rows[widest] > rows:
        raise ValueError(f"tile {tile}: column {widest} has chunk rows {chunk_rows[widest]}, above the slice bound {rows}")
    # Whole widest chunks keep each column whose chunk rows divide the step aligned.
    # Another column decodes a chunk twice at a boundary.
    step = rows // chunk_rows[widest] * chunk_rows[widest]
    for start in range(0, total, step):
        stop = min(start + step, total)

        def read(name):
            return read_rows(zf, f"grid/{tile}/{name}", start, stop, names=members)

        location = read("location")
        codeword = read("codeword_identity")[:, 0]
        cell = read("cell_id")
        n = stop - start
        x, y, z = location.T
        prefix, suffix = pa.array(cell[:, 0].astype(np.int64)), pa.array(cell[:, 1].astype(np.int64))
        yield pa.table(
            {
                "instance": pa.array(np.arange(first_instance + start, first_instance + stop, dtype=np.int64)),
                "grid_tile": pa.repeat(tile, n),
                "x": x,
                "y": y,
                "z": z,
                "codeword_index": codeword.astype(np.int64),
                "feature_name": pa.DictionaryArray.from_arrays(
                    features.indices.take(pa.array(codeword)), features.dictionary
                ),
                "cell_prefix": prefix,
                "dataset_suffix": suffix,
                "cell_id": cell_ids_arrow(prefix, suffix),
                "quality_score": read("quality_score")[:, 0].astype(np.float32),
                "overlaps_nucleus": read("overlaps_nucleus")[:, 0].astype(bool),
                "bbox": pa.StructArray.from_arrays(
                    [pa.array(v.astype(np.float64)) for v in (x, y, x, y)],
                    fields=list(BBOX),
                ),
            },
            schema=TRANSCRIPT_SCHEMA,
        )


def tile_table(
    zf: zipfile.ZipFile, tile: str, codewords: pa.Table, *, first_instance: int
) -> pa.Table:
    """The transcripts of ``grid/<tile>`` as one table; the whole tile is in memory."""
    return pa.concat_tables(tile_slices(zf, tile, codewords, first_instance=first_instance)).combine_chunks()


def write_tile(
    zf: zipfile.ZipFile,
    tile: str,
    codewords: pa.Table,
    path: Path,
    *,
    first_instance: int,
    field_ids: Mapping[str, int],
    rows: int = SLICE_ROWS,
) -> int:
    """Write ``grid/<tile>`` to the Parquet file ``path``, one row group per slice. Returns the row count."""
    schema = parquet_schema(TRANSCRIPT_SCHEMA, index_column="instance", field_ids=field_ids)
    count = 0
    with pq.ParquetWriter(path, schema) as writer:
        for piece in tile_slices(zf, tile, codewords, first_instance=first_instance, rows=rows):
            writer.write_table(piece.cast(schema), row_group_size=len(piece))
            count += len(piece)
    return count


def tile_order(grid_attrs: Mapping) -> list[tuple[str, int]]:
    """Each tile key with its first ``instance``, in the source's order: the rows of the tiles before it."""
    first, out = 0, []
    for key, count in zip(grid_attrs["grid_keys"], grid_attrs["grid_number_objects"], strict=True):
        out.append((key, first))
        first += count
    return out
