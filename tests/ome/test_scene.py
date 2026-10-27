import pytest

from minus80.ome import scene_attrs, well_attrs
from minus80.ome.image import validate

DIMS = ("c", "z", "y", "x")
UNITS = {"z": "micrometer", "y": "micrometer", "x": "micrometer"}


def test_each_image_is_translated_into_the_scene():
    attrs = scene_attrs({"0": {"c": 0, "z": 0, "y": 0, "x": 4326.4},
                         "1": {"c": 0, "z": 0, "y": 650, "x": 0}},
                        name="well", dims=DIMS, units=UNITS)
    scene = attrs["ome"]["scene"]
    assert [s["name"] for s in scene["coordinateSystems"]] == ["well"]
    assert [a["name"] for a in scene["coordinateSystems"][0]["axes"]] == list(DIMS)
    first = scene["coordinateTransformations"][0]
    assert first["type"] == "translation"
    assert list(first["translation"]) == [0, 0, 0, 4326.4]
    assert first["input"] == {"name": "intrinsic", "path": "0"}
    assert first["output"] == {"name": "well"}


def test_a_well_can_also_be_a_scene():
    well = well_attrs(["0", "1"], acquisitions=[0, 1], version="0.6")["ome"]
    scene = scene_attrs({"0": dict.fromkeys(DIMS, 0), "1": dict.fromkeys(DIMS, 0)},
                        name="well", dims=DIMS, units=UNITS)["ome"]
    both = well | {"scene": scene["scene"]}
    validate(both, "0.6", model="well")
    validate(both, "0.6", model="scene")


def test_every_axis_needs_a_translation():
    with pytest.raises(ValueError, match="no translation given"):
        scene_attrs({"0": {"y": 0, "x": 0}}, name="well", dims=DIMS, units=UNITS)
