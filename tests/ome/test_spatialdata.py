import numpy as np

from minus80.ome import image_attrs
from minus80.ome.spatialdata import (
    affine,
    container_attrs,
    points_attrs,
    raster_attrs,
    scale,
    sequence,
    shapes_attrs,
    table_attrs,
)


def test_scale_names_input_and_output_axes():
    t = scale([1.0, 0.2125, 0.2125], ("c", "y", "x"))
    assert t["input"]["axes"][0] == {"name": "c", "type": "channel"}
    assert t["output"] == {"name": "global", "axes": [{"name": "c", "type": "channel"}, {"name": "y", "type": "space"}, {"name": "x", "type": "space"}]}


def test_raster_attrs_sets_the_spatialdata_version_and_element_transform():
    ome = image_attrs(name="m", dims=("c", "y", "x"), level_shapes=[(4, 100, 200), (4, 50, 100)], scale={"c": 1, "y": 1, "x": 1})
    attrs = raster_attrs(ome, scale([1, 0.2, 0.2], ("c", "y", "x")))
    assert attrs["ome"]["version"] == "0.5-dev-spatialdata"
    assert attrs["ome"]["multiscales"][0]["coordinateTransformations"][0]["type"] == "scale"
    assert attrs["spatialdata_attrs"] == {"version": "0.3"}
    assert attrs["ome"]["multiscales"][0]["datasets"][1]["coordinateTransformations"][0]["scale"] == [1, 2, 2]
    assert "coordinateTransformations" not in ome["multiscales"][0]


def test_points_and_shapes_attrs_carry_the_spec_keys():
    p = points_attrs(("x", "y", "z"), scale([1, 1, 1], ("x", "y", "z")), feature_key="feature_name")
    assert p["encoding-type"] == "ngff:points" and p["spatialdata_attrs"] == {"version": "0.2", "feature_key": "feature_name"}
    s = shapes_attrs("POLYGON", scale([1, 1], ("x", "y")))
    assert s["spatialdata_attrs"]["geos"] == {"name": "POLYGON", "type": 3}
    assert shapes_attrs("POINT", scale([1, 1], ("x", "y")))["spatialdata_attrs"]["geos"] == {"name": "POINT", "type": 0}


def test_affine_drops_the_homogeneous_row():
    m = np.array([[0.0024, 1.289, 914.1], [-1.289, 0.0024, 32040.6], [0, 0, 1]])
    assert affine(m, ("x", "y"), ("x", "y"))["affine"] == m[:2].tolist()


def test_table_attrs():
    assert table_attrs("cell_labels", region_key="region", instance_key="instance")["spatialdata-encoding-type"] == "ngff:regions_table"


def test_container_attrs():
    assert container_attrs() == {"spatialdata_attrs": {"version": "0.2"}}


def test_sequence_lists_its_members():
    members = [scale([2, 2], ("x", "y")), scale([1, 1], ("x", "y"))]
    t = sequence(members, ("x", "y"))
    assert t["type"] == "sequence" and t["transformations"] == members
    assert t["output"]["name"] == "global"


def test_points_attrs_carry_both_keys_when_given():
    p = points_attrs(("x", "y"), scale([1, 1], ("x", "y")), instance_key="cell_id", feature_key="gene")
    assert p["spatialdata_attrs"] == {"version": "0.2", "instance_key": "cell_id", "feature_key": "gene"}


def test_table_attrs_keys():
    assert table_attrs("cell_labels", region_key="r", instance_key="i") == {
        "spatialdata-encoding-type": "ngff:regions_table", "region": "cell_labels",
        "region_key": "r", "instance_key": "i", "version": "0.2"}
