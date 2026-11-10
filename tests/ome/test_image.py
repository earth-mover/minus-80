import pytest

from minus80 import ome as vo

PYRAMID = dict(name="slide", dims=("c", "y", "x"),
               level_shapes=[(3, 1000, 2000), (3, 250, 500)],
               scale={"c": 1, "y": 0.25, "x": 0.25},
               units={"y": "micrometer", "x": "micrometer"})


def _transforms(attrs, level):
    return attrs["multiscales"][0]["datasets"][level]["coordinateTransformations"]


def test_no_translation_by_default():
    attrs = vo.image_attrs(**PYRAMID)
    assert [t["type"] for t in _transforms(attrs, 0)] == ["scale"]
    assert [t["type"] for t in _transforms(attrs, 1)] == ["scale"]


def test_translation_keeps_level_corners_together():
    attrs = vo.image_attrs(**PYRAMID, translation={"c": 0, "y": 0, "x": 0})
    assert _transforms(attrs, 0)[1] == {"type": "translation", "translation": [0, 0, 0]}
    # Level 1 pixels are 1.0 um against 0.25 um, so their centres sit 0.375 um in.
    assert _transforms(attrs, 1)[1]["translation"] == [0, 0.375, 0.375]


def test_translation_needs_every_axis():
    with pytest.raises(ValueError, match="no translation given"):
        vo.image_attrs(**PYRAMID, translation={"y": 0, "x": 0})


def test_translation_in_06_is_a_scale_then_translation_sequence():
    attrs = vo.image_attrs(**PYRAMID, translation={"c": 0, "y": 1, "x": 2}, version="0.6")
    (sequence,) = _transforms(attrs, 1)
    assert sequence["type"] == "sequence"
    assert [t["type"] for t in sequence["transformations"]] == ["scale", "translation"]
    assert sequence["transformations"][1]["translation"] == [0, 1.375, 2.375]


def test_channel_omero_signed_floor():
    omero = vo.channel_omero([{"label": "CT", "start": -160, "end": 240}],
                             max_value=32767, min_value=-32768)
    window = omero["channels"][0]["window"]
    assert window == {"min": -32768, "max": 32767, "start": -160, "end": 240}


def test_label_colors_and_properties():
    attrs = vo.label_attrs(
        name="organs", dims=("z", "y", "x"), level_shapes=[(4, 8, 8)],
        scale={"z": 2.0, "y": 0.5, "x": 0.5}, translation={"z": -100.0, "y": 1.0, "x": 2.0},
        colors=[{"label-value": 1, "rgba": [255, 0, 0, 255]}],
        properties=[{"label-value": 1, "name": "Liver", "code": "SCT:10200004"}])
    label = attrs["image-label"]
    assert label["colors"] == [{"label-value": 1, "rgba": [255, 0, 0, 255]}]
    assert label["properties"][0]["name"] == "Liver"


def test_label_without_colors_has_none():
    attrs = vo.label_attrs(name="l", dims=("y", "x"), level_shapes=[(8, 8)],
                           scale={"y": 1, "x": 1})
    assert "colors" not in attrs["image-label"]
    assert "properties" not in attrs["image-label"]


def _axes(attrs, version):
    multiscale = attrs["multiscales"][0]
    axes = multiscale["axes"] if version == "0.5" else multiscale["coordinateSystems"][0]["axes"]
    return [(a["name"], a["type"]) for a in axes]


@pytest.mark.parametrize("version", ["0.5", "0.6"])
def test_axis_types_come_from_dimensions(version):
    attrs = vo.image_attrs(name="fovs", dims=("fov", "z", "y", "x"),
                           level_shapes=[(2, 51, 600, 600)],
                           scale={"fov": 1, "z": 0.5, "y": 0.2, "x": 0.2},
                           dimensions={"fov": {"type": "fov"}, "z": {"type": "space"}},
                           version=version)
    assert _axes(attrs, version) == [("fov", "fov"), ("z", "space"), ("y", "space"),
                                     ("x", "space")]


@pytest.mark.parametrize("version", ["0.5", "0.6"])
def test_a_custom_axis_beside_channel_raises(version):
    with pytest.raises(ValueError, match="more than one channel or custom-typed axis"):
        vo.image_attrs(name="fovs", dims=("fov", "c", "z", "y", "x"),
                       level_shapes=[(2, 2, 51, 600, 600)],
                       scale={"fov": 1, "c": 1, "z": 0.5, "y": 0.2, "x": 0.2},
                       dimensions={"fov": {"type": "fov"}}, version=version)


@pytest.mark.parametrize(("dims", "rule"), [
    (("t", "time", "y", "x"), "more than one of type time"),
    (("x",), "1 of type space"),
    (("t", "z", "w", "y", "x"), "4 of type space"),
    (("c", "t", "y", "x"), "out of order"),
    (("y", "c", "x"), "out of order"),
])
def test_axis_rules_raise(dims, rule):
    with pytest.raises(ValueError, match=rule):
        vo.image_attrs(name="bad", dims=dims, level_shapes=[(2,) * len(dims)],
                       scale=dict.fromkeys(dims, 1), dimensions={"w": {"type": "space"}})


def test_label_axis_rules_raise():
    with pytest.raises(ValueError, match="out of order"):
        vo.label_attrs(name="l", dims=("y", "x", "c"), level_shapes=[(8, 8, 2)],
                       scale={"y": 1, "x": 1, "c": 1})


@pytest.mark.parametrize("version", ["0.5", "0.6"])
def test_time_named_in_full_is_time(version):
    attrs = vo.image_attrs(name="movie", dims=("time", "y", "x"), level_shapes=[(10, 8, 8)],
                           scale={"time": 1, "y": 1, "x": 1}, version=version)
    assert _axes(attrs, version)[0] == ("time", "time")


def test_dimensions_override_the_name():
    assert vo.axis_types(("z", "y", "x"), {"z": {"type": "time"}}) == ["time", "space", "space"]


def test_an_untyped_unknown_axis_raises():
    with pytest.raises(ValueError, match="'image'"):
        vo.image_attrs(name="stack", dims=("image", "c", "y", "x"),
                       level_shapes=[(10, 2, 64, 64)],
                       scale={"image": 1, "c": 1, "y": 1, "x": 1})


def test_label_axis_types_come_from_dimensions():
    attrs = vo.label_attrs(name="l", dims=("fov", "y", "x"), level_shapes=[(2, 8, 8)],
                           scale={"fov": 1, "y": 1, "x": 1},
                           dimensions={"fov": {"type": "fov"}})
    assert _axes(attrs, "0.5")[0] == ("fov", "fov")


FIELD = dict(name="field", dims=("y", "x"), level_shapes=[(1080, 1280), (68, 80)],
             units={"y": "micrometer", "x": "micrometer"}, downsampling="mean")
FACTORS = [{"y": 1, "x": 1}, {"y": 16, "x": 16}]


def test_shape_ratio_misstates_a_partial_block_level():
    attrs = vo.image_attrs(**FIELD, scale={"y": 0.5, "x": 0.5})
    y, x = _transforms(attrs, 1)[0]["scale"]
    assert y == pytest.approx(0.5 * 1080 / 68) and y != x


@pytest.mark.parametrize("version", ["0.5", "0.6"])
@pytest.mark.parametrize("origin", [None, {"y": 3.0, "x": -2.0}])
def test_level_factors_state_scale_and_mean_translation_exactly(version, origin):
    attrs = vo.image_attrs(**FIELD, scale={"y": 0.5, "x": 0.5}, level_factors=FACTORS,
                           translation=origin, version=version)
    datasets = attrs["multiscales"][0]["datasets"]
    if version == "0.6":
        transforms = [d["coordinateTransformations"][0] for d in datasets]
        scales = [t["transformations"][0] if t["type"] == "sequence" else t for t in transforms]
        shifts = [t["transformations"][1]["translation"] if t["type"] == "sequence" else None
                  for t in transforms]
    else:
        scales = [d["coordinateTransformations"][0] for d in datasets]
        shifts = [d["coordinateTransformations"][1]["translation"]
                  if len(d["coordinateTransformations"]) > 1 else None for d in datasets]
    assert scales[1]["scale"] == [8.0, 8.0]
    t0 = origin or {"y": 0.0, "x": 0.0}
    assert shifts[1] == [t0["y"] + 3.75, t0["x"] + 3.75]
    assert shifts[0] == (None if origin is None else [3.0, -2.0])


def test_level_factors_without_a_mean_add_no_translation():
    attrs = vo.image_attrs(**{**FIELD, "downsampling": "unknown"}, scale={"y": 0.5, "x": 0.5},
                           level_factors=FACTORS)
    assert [len(_transforms(attrs, i)) for i in (0, 1)] == [1, 1]


def test_level_factors_must_match_levels_and_start_at_one():
    with pytest.raises(ValueError, match="level_factors"):
        vo.image_attrs(**FIELD, scale={"y": 1, "x": 1}, level_factors=FACTORS[:1])
    with pytest.raises(ValueError, match="relative to level 0"):
        vo.image_attrs(**FIELD, scale={"y": 1, "x": 1}, level_factors=[FACTORS[1], FACTORS[1]])
