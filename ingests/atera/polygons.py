"""One polygon set of an Atera ``cells.zarr.zip`` as an Arrow table."""

from __future__ import annotations

import zipfile

import numpy as np
import pyarrow as pa
import shapely
from bundle import read_v2_array

_BBOX = pa.struct([(n, pa.float64()) for n in ("xmin", "ymin", "xmax", "ymax")])

# Iceberg has no unsigned type, so the source's uint32 columns are int64 here.
POLYGON_SCHEMA = pa.schema(
    [
        ("instance", pa.int64()),
        ("cell_index", pa.int64()),
        ("cell_id", pa.string()),
        ("method", pa.int64()),
        ("geometry", pa.binary()),
        ("bbox", _BBOX),
    ]
)

GEO_POLYGONS = {
    "version": "1.1.0",
    "primary_column": "geometry",
    "columns": {
        "geometry": {
            "encoding": "WKB",
            "geometry_types": ["Polygon"],
            "covering": {
                "bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}
            },
        }
    },
}


def polygon_table(zf: zipfile.ZipFile, set_index: int, cell_ids: np.ndarray) -> pa.Table:
    """Polygon set ``set_index``; ``cell_ids`` holds one Xenium id per cell, indexed by ``cell_index``.

    ``vertices`` rows interleave x and y (x0, y0, x1, y1, ...); ``num_vertices`` counts the pairs.
    A ring with under 3 vertices or a non-finite coordinate raises ``ValueError`` naming the row.
    An invalid ring, such as a bow-tie, is kept as published: the source's errors are reproduced.
    """
    read = lambda name: read_v2_array(zf, f"polygon_sets/{set_index}/{name}")
    vertices, num_vertices = read("vertices"), read("num_vertices")
    cell_index, method = read("cell_index"), read("method")
    polygons = []
    for i, (row, count) in enumerate(zip(vertices, num_vertices)):
        ring = row[: 2 * count].reshape(count, 2).astype(np.float64)
        if count < 3 or not np.isfinite(ring).all():
            raise ValueError(f"polygon row {i} has {count} vertices and cannot be a ring")
        polygons.append(shapely.Polygon(ring))
    bounds = shapely.bounds(polygons) if polygons else np.empty((0, 4))
    return pa.table(
        {
            "instance": pa.array(cell_index.astype(np.int64) + 1),
            "cell_index": cell_index.astype(np.int64),
            "cell_id": pa.array(cell_ids[cell_index], type=pa.string()),
            "method": method.astype(np.int64),
            "geometry": pa.array(shapely.to_wkb(polygons), type=pa.binary()),
            "bbox": pa.StructArray.from_arrays(
                [pa.array(bounds[:, i]) for i in range(4)], fields=list(_BBOX)
            ),
        },
        schema=POLYGON_SCHEMA,
    )
