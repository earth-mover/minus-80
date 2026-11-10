"""One polygon set of an Atera ``cells.zarr.zip`` as an Arrow table."""

from __future__ import annotations

import zipfile

import numpy as np
import pyarrow as pa
import shapely
from bundle import read_v2_array

_BBOX = pa.struct([(n, pa.float64()) for n in ("xmin", "ymin", "xmax", "ymax")])

POLYGON_SCHEMA = pa.schema(
    [
        ("instance", pa.int64()),
        ("cell_index", pa.uint32()),
        ("cell_id", pa.string()),
        ("method", pa.uint32()),
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
    """
    read = lambda name: read_v2_array(zf, f"polygon_sets/{set_index}/{name}")
    vertices, num_vertices = read("vertices"), read("num_vertices")
    cell_index, method = read("cell_index"), read("method")
    polygons = [
        shapely.Polygon(row[: 2 * count].reshape(count, 2).astype(np.float64))
        for row, count in zip(vertices, num_vertices)
    ]
    bounds = shapely.bounds(polygons) if polygons else np.empty((0, 4))
    return pa.table(
        {
            "instance": pa.array(cell_index.astype(np.int64) + 1),
            "cell_index": cell_index,
            "cell_id": pa.array(cell_ids[cell_index], type=pa.string()),
            "method": method,
            "geometry": pa.array(shapely.to_wkb(polygons), type=pa.binary()),
            "bbox": pa.StructArray.from_arrays(
                [pa.array(bounds[:, i]) for i in range(4)], fields=list(_BBOX)
            ),
        },
        schema=POLYGON_SCHEMA,
    )
