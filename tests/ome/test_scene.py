import pytest

from minus80.ome import scene_attrs, well_attrs
from minus80.ome.image import validate

DIMS = ("c", "z", "y", "x")
UNITS = {"z": "micrometer", "y": "micrometer", "x": "micrometer"}


def test_each_image_is_translated_into_the_scene():
    attrs = scene_attrs(
        {"0": {"c": 0, "z": 0, "y": 0, "x": 4326.4}, "1": {"c": 0, "z": 0, "y": 650, "x": 0}},
        name="well",
        dims=DIMS,
        units=UNITS,
    )
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
    scene = scene_attrs(
        {"0": dict.fromkeys(DIMS, 0), "1": dict.fromkeys(DIMS, 0)},
        name="well",
        dims=DIMS,
        units=UNITS,
    )["ome"]
    both = well | {"scene": scene["scene"]}
    validate(both, "0.6", model="well")
    validate(both, "0.6", model="scene")


def test_every_axis_needs_a_translation():
    with pytest.raises(ValueError, match="no translation given"):
        scene_attrs({"0": {"y": 0, "x": 0}}, name="well", dims=DIMS, units=UNITS)


def _image(group, name):
    """A two-level (c, z, y, x) image named ``name`` under ``group``."""
    import numpy as np

    from minus80.ome import image_attrs

    image = group.require_group(name)
    shapes = [(1, 4, 16, 16), (1, 4, 8, 8)]
    image.attrs["ome"] = image_attrs(name=name, dims=DIMS, level_shapes=shapes,
                                     scale={"c": 1, "z": 1, "y": 0.5, "x": 0.5}, units=UNITS, version="0.6")
    for i, shape in enumerate(shapes):
        image.create_array(str(i), data=np.zeros(shape, "u2"), dimension_names=DIMS)


def _scans(fields):
    """A plain root holding one scan, a 0.6 scene that names ``fields`` and holds field-1 and field-2."""
    import zarr

    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    scan = root.create_group("scans/a")
    scan.attrs.update(scene_attrs({f: dict.fromkeys(DIMS, 0) for f in fields}, name="stage", dims=DIMS,
                                  units=UNITS))
    for field in ("field-1", "field-2"):
        _image(scan, field)
    return root


def test_a_scene_declares_its_images_at_any_depth():
    from minus80.ome.hierarchy import declared, scene_images
    from minus80.ome.verify import schema, series_matches

    root = _scans(["field-1", "field-2"])
    assert declared(root) == ["scans/a/field-1", "scans/a/field-2"]
    assert scene_images(root) == {"scans/a": ["scans/a/field-1", "scans/a/field-2"]}
    assert series_matches(root) == [] and schema(root, "0.6") == []


def test_a_scene_must_name_exactly_the_images_under_it():
    from minus80.ome.verify import series_matches

    problems = series_matches(_scans(["field-1", "field-3"]))
    assert any("does not hold" in p and "field-3" in p for p in problems)
    assert any("does not name" in p and "field-2" in p for p in problems)
