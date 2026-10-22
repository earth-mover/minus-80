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


@pytest.mark.parametrize("version", ["0.5", "0.6"])
def test_image_axis_is_typed_custom(version):
    attrs = vo.image_attrs(name="stack", dims=("image", "c", "y", "x"),
                           level_shapes=[(10, 2, 64, 64)],
                           scale={"image": 1, "c": 1, "y": 1, "x": 1}, version=version)
    multiscale = attrs["multiscales"][0]
    axes = multiscale["axes"] if version == "0.5" else multiscale["coordinateSystems"][0]["axes"]
    assert [a["type"] for a in axes] == ["custom", "channel", "space", "space"]
