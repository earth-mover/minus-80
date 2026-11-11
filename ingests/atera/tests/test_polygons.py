import numpy as np
import pyarrow as pa
import pytest
import shapely

from polygons import GEO_POLYGONS, POLYGON_SCHEMA, polygon_table
from .stores import zip_v2_store


@pytest.fixture
def zf(tmp_path):
    # x and y interleave per vertex: x0, y0, x1, y1, ...
    vertices = np.zeros((2, 8), dtype="<f4")
    vertices[0, :8] = [0, 0, 1, 0, 1, 1, 0, 1]
    vertices[1, :6] = [2, 2, 4, 2, 3, 4]
    return zip_v2_store(
        tmp_path,
        "cells",
        {
            "polygon_sets/0/vertices": (vertices, {}),
            "polygon_sets/0/num_vertices": (np.array([4, 3], dtype="<i4"), {}),
            "polygon_sets/0/cell_index": (np.array([0, 1], dtype="<u4"), {}),
            "polygon_sets/0/method": (np.array([1, 1], dtype="<u4"), {}),
        },
    )


def test_polygon_table_closes_rings_and_numbers_instances(zf):
    cell_ids = np.array(["aaaaaaab-1", "aaaaaaac-1"])
    p = polygon_table(zf, 0, cell_ids)
    assert p.schema.equals(POLYGON_SCHEMA)
    assert POLYGON_SCHEMA.field("cell_index").type == pa.int64()
    assert POLYGON_SCHEMA.field("method").type == pa.int64()
    assert p["instance"].to_pylist() == [1, 2]
    assert p["cell_id"].to_pylist() == ["aaaaaaab-1", "aaaaaaac-1"]
    poly = shapely.from_wkb(p["geometry"][0].as_py())
    assert poly.is_valid and poly.area == 1.0 and p["bbox"][0].as_py()["xmax"] == 1.0
    triangle = shapely.from_wkb(p["geometry"][1].as_py())
    assert triangle.area == 2.0
    assert p["bbox"][1].as_py() == {"xmin": 2.0, "ymin": 2.0, "xmax": 4.0, "ymax": 4.0}


def test_geo_polygons_declares_a_wkb_polygon_column():
    assert GEO_POLYGONS["primary_column"] == "geometry"
    assert GEO_POLYGONS["columns"]["geometry"]["encoding"] == "WKB"
    assert GEO_POLYGONS["columns"]["geometry"]["geometry_types"] == ["Polygon"]


def _set(tmp_path, rows):
    vertices = np.zeros((len(rows), 10), dtype="<f4")
    for i, row in enumerate(rows):
        vertices[i, : len(row)] = row
    return zip_v2_store(
        tmp_path,
        "cells",
        {
            "polygon_sets/0/vertices": (vertices, {}),
            "polygon_sets/0/num_vertices": (np.array([len(r) // 2 for r in rows], dtype="<i4"), {}),
            "polygon_sets/0/cell_index": (np.arange(len(rows), dtype="<u4"), {}),
            "polygon_sets/0/method": (np.ones(len(rows), dtype="<u4"), {}),
        },
    )


@pytest.mark.parametrize("row", [[0, 0, 1, 1], []])
def test_polygon_table_rejects_rings_under_three_vertices(tmp_path, row):
    zf = _set(tmp_path, [[0, 0, 1, 0, 1, 1], row])
    with pytest.raises(ValueError, match=r"row 1 has %d vertices" % (len(row) // 2)):
        polygon_table(zf, 0, np.array(["a", "b"]))


def test_polygon_table_rejects_non_finite_coordinates(tmp_path):
    zf = _set(tmp_path, [[0, 0, np.nan, 0, 1, 1]])
    with pytest.raises(ValueError, match="row 0"):
        polygon_table(zf, 0, np.array(["a"]))


def test_closed_and_open_rings_give_the_same_polygon(tmp_path):
    zf = _set(tmp_path, [[0, 0, 1, 0, 1, 1, 0, 1], [0, 0, 1, 0, 1, 1, 0, 1, 0, 0]])
    p = polygon_table(zf, 0, np.array(["a", "b"]))
    a, b = (shapely.from_wkb(p["geometry"][i].as_py()) for i in (0, 1))
    assert a.equals(b) and a.area == b.area == 1.0


def test_self_intersecting_ring_is_kept_as_published(tmp_path):
    zf = _set(tmp_path, [[0, 0, 1, 1, 1, 0, 0, 1]])
    p = polygon_table(zf, 0, np.array(["a"]))
    assert not shapely.from_wkb(p["geometry"][0].as_py()).is_valid
