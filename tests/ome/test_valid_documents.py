"""Every document shape the builders produce passes the spec's JSON schema.

The builders do not validate each call (see :mod:`minus80.ome.image`), so this
is where a builder that writes an invalid document is caught.
"""

import pytest

from minus80 import ome
from minus80.ome.image import channel_omero, label_attrs, rgb_omero, validate
from minus80.ome.plate import plate_attrs, plate_grid, well_attrs

VERSIONS = ["0.5", "0.6"]
UNITS = {"y": "micrometer", "x": "micrometer"}


def image_cases():
    yield "image", dict(name="i", dims=("y", "x"), level_shapes=[(64, 64)], scale={"y": 0.5, "x": 0.5}, units=UNITS)
    yield "image", dict(name="i", dims=("c", "y", "x"), level_shapes=[(3, 64, 64), (3, 32, 32)],
                        scale={"c": 1, "y": 0.5, "x": 0.5}, units=UNITS, omero=rgb_omero())
    yield "image", dict(name="i", dims=("t", "c", "z", "y", "x"), level_shapes=[(2, 2, 4, 64, 64)],
                        scale={"t": 1, "c": 1, "z": 2, "y": 0.5, "x": 0.5},
                        units={"z": "micrometer", **UNITS}, translation={"t": 0, "c": 0, "z": 1, "y": 3, "x": 4},
                        omero=channel_omero([{"label": "DNA", "color": "0000FF"}, {"label": "Actin"}], max_value=255))


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("model, kwargs", list(image_cases()))
def test_images_validate(version, model, kwargs):
    validate(ome.image_attrs(**kwargs, version=version), version, model=model, strict=True)


@pytest.mark.parametrize("version", VERSIONS)
def test_labels_validate(version):
    label = label_attrs(name="cells", dims=("y", "x"), level_shapes=[(8, 8)], scale={"y": 1, "x": 1},
                        colors=[{"label-value": 1, "rgba": [255, 0, 0, 255]}], version=version)
    validate(label, version, model="label", strict=True)


@pytest.mark.parametrize("version", VERSIONS)
def test_plates_and_wells_validate(version):
    rows, columns = plate_grid(1536)
    plate = plate_attrs(name="p", rows=rows, columns=columns, wells=[("B", "2"), ("AE", "47")],
                        field_count=4, version=version)
    validate(plate["ome"], version, model="plate")
    validate(well_attrs(["1", "2", "3", "4"], version=version)["ome"], version, model="well")
    validate(well_attrs(["0", "1"], acquisitions=[0, 1], version=version)["ome"], version, model="well")


@pytest.mark.parametrize("version", VERSIONS)
def test_collections_validate(version):
    validate(ome.root_attrs(version=version)["ome"], version, model="bf2raw")


def test_scenes_validate():
    dims = ("c", "y", "x")
    scene = ome.scene_attrs({"0": {"c": 0, "y": 0, "x": 10.5}}, name="well", dims=dims, units=UNITS)
    validate(scene["ome"], "0.6", model="scene")
