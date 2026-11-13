import numpy as np

import pytest
import zarr

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
    translation,
    write_table_keys,
)


def test_scale_names_input_and_output_axes():
    t = scale([1.0, 0.2125, 0.2125], ("c", "y", "x"), unit="micrometer")
    assert t["input"]["axes"][0] == {"name": "c", "type": "channel"}
    assert t["output"] == {"name": "global", "axes": [{"name": "c", "type": "channel"}, {"name": "y", "type": "space", "unit": "micrometer"}, {"name": "x", "type": "space", "unit": "micrometer"}]}


def test_every_space_axis_has_a_unit_because_spatialdata_refuses_one_without():
    t = scale([0.2125, 0.2125], ("y", "x"), unit="micrometer")
    assert [a["unit"] for a in t["input"]["axes"]] == ["unit", "unit"]
    assert [a["unit"] for a in t["output"]["axes"]] == ["micrometer", "micrometer"]
    a = affine(np.eye(3), ("y", "x"), ("y", "x"), unit="micrometer")
    assert [ax["unit"] for ax in a["output"]["axes"]] == ["micrometer", "micrometer"]
    q = sequence([t], ("y", "x"), unit="micrometer")
    assert [ax["unit"] for ax in q["output"]["axes"]] == ["micrometer", "micrometer"]


def test_a_transform_can_start_in_the_system_that_another_ends_in():
    first = affine(np.eye(3), ("x", "y"), ("x", "y"), system="morphology_pixel", unit="pixel")
    second = scale([2.0, 2.0], ("x", "y"), input_system="morphology_pixel", input_unit="pixel", unit="micrometer")
    assert first["output"] == second["input"]
    assert [a["unit"] for a in first["output"]["axes"]] == ["pixel", "pixel"]
    assert second["output"]["name"] == "global"


def test_translation_follows_a_scale_in_a_sequence_and_leaves_the_channel_at_zero():
    first = scale([1.0, 20.0, 20.0], ("c", "y", "x"), system="grid", unit="micrometer")
    second = translation([0.0, -10.0, -10.0], ("c", "y", "x"), input_system="grid", input_unit="micrometer",
                         unit="micrometer")
    assert second["type"] == "translation" and second["translation"] == [0.0, -10.0, -10.0]
    assert first["output"] == second["input"]
    assert second["output"]["name"] == "global"


def test_raster_attrs_refuses_a_document_that_is_not_the_0_5_layout():
    ome = image_attrs(name="m", dims=("y", "x"), level_shapes=[(100, 200)], scale={"y": 1, "x": 1}, version="0.6")
    with pytest.raises(ValueError, match="0.5"):
        raster_attrs(ome, scale([0.2, 0.2], ("y", "x"), unit="micrometer"))


def test_raster_attrs_sets_the_spatialdata_version_and_element_transform():
    ome = image_attrs(name="m", dims=("c", "y", "x"), level_shapes=[(4, 100, 200), (4, 50, 100)], scale={"c": 1, "y": 1, "x": 1},
                      version="0.5")
    attrs = raster_attrs(ome, scale([1, 0.2, 0.2], ("c", "y", "x"), unit="micrometer"))
    assert [a["name"] for a in attrs["ome"]["multiscales"][0]["axes"]] == ["c", "y", "x"]
    assert attrs["ome"]["version"] == "0.5-dev-spatialdata"
    assert attrs["ome"]["multiscales"][0]["coordinateTransformations"][0]["type"] == "scale"
    assert attrs["spatialdata_attrs"] == {"version": "0.3"}
    assert attrs["ome"]["multiscales"][0]["datasets"][1]["coordinateTransformations"][0]["scale"] == [1, 2, 2]
    assert "coordinateTransformations" not in ome["multiscales"][0]


def test_points_and_shapes_attrs_carry_the_spec_keys():
    p = points_attrs(("x", "y", "z"), scale([1, 1, 1], ("x", "y", "z"), unit="micrometer"), feature_key="feature_name")
    assert p["encoding-type"] == "ngff:points" and p["spatialdata_attrs"] == {"version": "0.2", "feature_key": "feature_name"}
    s = shapes_attrs("POLYGON", scale([1, 1], ("x", "y"), unit="micrometer"))
    assert s["spatialdata_attrs"]["geos"] == {"name": "POLYGON", "type": 3}
    assert shapes_attrs("POINT", scale([1, 1], ("x", "y"), unit="micrometer"))["spatialdata_attrs"]["geos"] == {"name": "POINT", "type": 0}


def test_affine_drops_the_homogeneous_row():
    m = np.array([[0.0024, 1.289, 914.1], [-1.289, 0.0024, 32040.6], [0, 0, 1]])
    assert affine(m, ("x", "y"), ("x", "y"), unit="micrometer")["affine"] == m[:2].tolist()


def test_table_attrs():
    assert table_attrs("cell_labels", region_key="region", instance_key="instance")["spatialdata-encoding-type"] == "ngff:regions_table"


def test_container_attrs():
    assert container_attrs() == {"spatialdata_attrs": {"version": "0.2"}}


def test_sequence_lists_its_members():
    members = [scale([2, 2], ("x", "y"), unit="micrometer"), scale([1, 1], ("x", "y"), unit="micrometer")]
    t = sequence(members, ("x", "y"), unit="micrometer")
    assert t["type"] == "sequence" and t["transformations"] == members
    assert t["output"]["name"] == "global"


def test_points_attrs_carry_both_keys_when_given():
    p = points_attrs(("x", "y"), scale([1, 1], ("x", "y"), unit="micrometer"), instance_key="cell_id", feature_key="gene")
    assert p["spatialdata_attrs"] == {"version": "0.2", "instance_key": "cell_id", "feature_key": "gene"}


def test_table_attrs_keys():
    assert table_attrs("cell_labels", region_key="r", instance_key="i") == {
        "spatialdata-encoding-type": "ngff:regions_table", "region": "cell_labels",
        "region_key": "r", "instance_key": "i", "version": "0.2"}


@pytest.mark.parametrize("transform", [scale, sequence])
def test_a_transform_without_a_unit_is_refused(transform):
    with pytest.raises(TypeError, match="unit"):
        transform([1, 1], ("x", "y"))


def test_an_affine_without_a_unit_is_refused():
    with pytest.raises(TypeError, match="unit"):
        affine(np.eye(3), ("x", "y"), ("x", "y"))


def test_write_table_keys_writes_the_group_attributes_and_uns():
    anndata_io = pytest.importorskip("anndata.io")
    group = zarr.open_group(store={}, mode="w")
    write_table_keys(group, "cell_boundaries", region_key="region", instance_key="instance")
    assert dict(group.attrs) == table_attrs("cell_boundaries", region_key="region", instance_key="instance")
    # SpatialData reads the keys from uns, as AnnData decodes it.
    assert anndata_io.read_elem(group["uns"]) == {"spatialdata_attrs": {
        "region": "cell_boundaries", "region_key": "region", "instance_key": "instance"}}
