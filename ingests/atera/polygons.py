"""One polygon set of an Atera ``cells.zarr.zip`` as an Arrow table."""

from __future__ import annotations

import zipfile

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import shapely
from minus80.core.zarr_copy import read_array

#: A row's bounds, so that column statistics can prune files.
BBOX = pa.struct([(n, pa.float64()) for n in ("xmin", "ymin", "xmax", "ymax")])

# Iceberg has no unsigned type, so the source's uint32 columns are int64 here.
POLYGON_SCHEMA = pa.schema(
    [
        pa.field("instance", pa.int64(), nullable=False),
        ("cell_index", pa.int64()),
        ("cell_id", pa.string()),
        ("part_count", pa.int32()),
        ("methods", pa.string()),
        ("geometry", pa.binary()),
        ("bbox", BBOX),
    ]
)

GEO_POLYGONS = {
    "version": "1.1.0",
    "primary_column": "geometry",
    "columns": {
        "geometry": {
            "encoding": "WKB",
            "geometry_types": ["Polygon", "MultiPolygon"],
            "covering": {
                "bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}
            },
        }
    },
}


def polygon_table(zf: zipfile.ZipFile, set_index: int, cell_ids: np.ndarray) -> pa.Table:
    """Polygon set ``set_index`` as one row per cell; ``cell_ids`` holds one Xenium id per cell, indexed by ``cell_index``.

    ``vertices`` rows interleave x and y (x0, y0, x1, y1, ...); ``num_vertices`` counts the pairs.
    A cell with one part is a ``Polygon``, a cell with several a ``MultiPolygon`` in source order.
    A ring with under 3 vertices, more vertices than its row holds, or a non-finite coordinate raises ``ValueError`` with the row.
    The function keeps an invalid ring, such as a bow-tie, as published: it reproduces the source's errors.
    """
    names = set(zf.namelist())

    def read(name):
        return read_array(zf, f"polygon_sets/{set_index}/{name}", names=names)

    vertices, num_vertices = read("vertices"), read("num_vertices").astype(np.int64)
    cell_index, method = read("cell_index").astype(np.int64), read("method")
    capacity = vertices.shape[1] // 2
    if len(over := np.flatnonzero(num_vertices > capacity)):
        raise ValueError(f"polygon row {over[0]} has {num_vertices[over[0]]} vertices, the row holds {capacity}")
    used = np.arange(vertices.shape[1])[None, :] < 2 * num_vertices[:, None]
    if len(bad := np.flatnonzero((num_vertices < 3) | (~np.isfinite(vertices) & used).any(axis=1))):
        raise ValueError(f"polygon row {bad[0]} has {num_vertices[bad[0]]} vertices and cannot be a ring")
    # Boolean indexing keeps the row-major order, so the rings follow one another in the flat array.
    coordinates = vertices[used].reshape(-1, 2).astype(np.float64)
    rings = shapely.linearrings(coordinates, indices=np.repeat(np.arange(len(num_vertices)), num_vertices))
    order = np.argsort(cell_index, kind="stable")
    parts = shapely.polygons(rings)[order]
    cells, first, part_count = np.unique(cell_index[order], return_index=True, return_counts=True)
    geometry = parts[first]
    several = part_count > 1
    if several.any():
        in_several = np.repeat(several, part_count)
        geometry[several] = shapely.multipolygons(
            parts[in_several], indices=np.repeat(np.arange(several.sum()), part_count[several])
        )
    offsets = np.concatenate([[0], np.cumsum(part_count)])
    methods = pc.binary_join(pa.ListArray.from_arrays(offsets, pa.array(method[order].astype(str))), ",")
    bounds = shapely.bounds(geometry) if len(geometry) else np.empty((0, 4))
    return pa.table(
        {
            "instance": pa.array(cells + 1),
            "cell_index": pa.array(cells),
            "cell_id": pa.array(cell_ids[cells], type=pa.string()),
            "part_count": pa.array(part_count.astype(np.int32)),
            "methods": methods,
            "geometry": pa.array(shapely.to_wkb(geometry), type=pa.binary()),
            "bbox": pa.StructArray.from_arrays(
                [pa.array(bounds[:, i]) for i in range(4)], fields=list(BBOX)
            ),
        },
        schema=POLYGON_SCHEMA,
    )
