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
    assert not POLYGON_SCHEMA.field("instance").nullable
    assert POLYGON_SCHEMA.field("cell_index").type == pa.int64()
    assert POLYGON_SCHEMA.field("part_count").type == pa.int32()
    assert POLYGON_SCHEMA.field("methods").type == pa.string()
    assert p["part_count"].to_pylist() == [1, 1]
    assert p["methods"].to_pylist() == ["1", "1"]
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
    assert GEO_POLYGONS["columns"]["geometry"]["geometry_types"] == ["Polygon", "MultiPolygon"]


def _set(tmp_path, rows, cell_index=None, method=None):
    vertices = np.zeros((len(rows), 10), dtype="<f4")
    for i, row in enumerate(rows):
        vertices[i, : len(row)] = row
    return zip_v2_store(
        tmp_path,
        "cells",
        {
            "polygon_sets/0/vertices": (vertices, {}),
            "polygon_sets/0/num_vertices": (np.array([len(r) // 2 for r in rows], dtype="<i4"), {}),
            "polygon_sets/0/cell_index": (np.array(range(len(rows)) if cell_index is None else cell_index, dtype="<u4"), {}),
            "polygon_sets/0/method": (np.ones(len(rows), dtype="<u4") if method is None else np.array(method, dtype="<u4"), {}),
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


SQUARE, TRIANGLE = [0, 0, 1, 0, 1, 1, 0, 1], [2, 2, 4, 2, 3, 4]


def _geometry(table, i):
    return shapely.from_wkb(table["geometry"][i].as_py())


def test_a_cell_without_a_polygon_leaves_a_gap(tmp_path):
    zf = _set(tmp_path, [SQUARE, TRIANGLE], cell_index=[0, 2])
    p = polygon_table(zf, 0, np.array(["a", "b", "c"]))
    assert p["instance"].to_pylist() == [1, 3]
    assert p["cell_index"].to_pylist() == [0, 2]
    assert p["cell_id"].to_pylist() == ["a", "c"]


def test_polygons_of_one_cell_merge_into_one_row_in_source_order(tmp_path):
    zf = _set(tmp_path, [SQUARE, TRIANGLE, SQUARE], cell_index=[0, 0, 1], method=[3, 4, 3])
    p = polygon_table(zf, 0, np.array(["a", "b"]))
    assert p["instance"].to_pylist() == [1, 2]
    assert p["part_count"].to_pylist() == [2, 1]
    assert p["methods"].to_pylist() == ["3,4", "3"]
    assert [_geometry(p, i).geom_type for i in (0, 1)] == ["MultiPolygon", "Polygon"]
    assert [g.area for g in _geometry(p, 0).geoms] == [1.0, 2.0]
    assert p["bbox"][0].as_py() == {"xmin": 0.0, "ymin": 0.0, "xmax": 4.0, "ymax": 4.0}


def test_source_rows_out_of_cell_order_keep_their_order_within_a_cell(tmp_path):
    zf = _set(tmp_path, [TRIANGLE, SQUARE, SQUARE], cell_index=[1, 0, 1], method=[4, 3, 5])
    p = polygon_table(zf, 0, np.array(["a", "b"]))
    assert p["cell_index"].to_pylist() == [0, 1]
    assert p["methods"].to_pylist() == ["3", "4,5"]
    assert [g.area for g in _geometry(p, 1).geoms] == [2.0, 1.0]


def test_vectorized_polygons_equal_the_loop_form(tmp_path):
    rows = [SQUARE, TRIANGLE, [0, 0, 1, 1, 1, 0, 0, 1], SQUARE + [0, 0]]
    p = polygon_table(_set(tmp_path, rows), 0, np.array(list("abcd")))
    loop = []
    for row in rows:
        ring = np.array(row, dtype=np.float32).reshape(-1, 2).astype(np.float64)
        loop.append(shapely.to_wkb(shapely.Polygon(ring)))
    assert [g.as_py() for g in p["geometry"]] == loop


def test_an_empty_set_gives_an_empty_table(tmp_path):
    zf = _set(tmp_path, [], cell_index=[])
    p = polygon_table(zf, 0, np.array(["a"]))
    assert p.schema.equals(POLYGON_SCHEMA) and len(p) == 0
