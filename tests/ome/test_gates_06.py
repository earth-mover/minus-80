"""The shared gates read 0.6 images, whose levels carry a scale-then-translation sequence."""

import numpy as np
import pytest
import zarr

from minus80.ome import image_attrs, root_attrs, series_attrs
from minus80.ome.hierarchy import images
from minus80.ome.verify import levels_aligned, physical_size

UNITS = {"y": "micrometer", "x": "micrometer"}


def _collection(version, level_shapes, **attrs):
    """A one-image collection whose levels hold a gradient, each level the one above averaged down."""
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.attrs.update(root_attrs(version))
    root.create_group("OME").attrs.update(series_attrs(["img"], version=version))
    image = root.create_group("img")
    image.attrs["ome"] = image_attrs(
        name="img",
        dims=("y", "x"),
        level_shapes=level_shapes,
        units=UNITS,
        version=version,
        **attrs,
    )
    for i, shape in enumerate(level_shapes):
        image.create_array(
            str(i), data=np.indices(shape).sum(0).astype("u2"), dimension_names=("y", "x")
        )
    assert list(images(root)) == ["img"]
    return root


@pytest.mark.parametrize("version", ["0.5", "0.6"])
def test_scale_is_found_in_either_layout(version):
    root = _collection(
        version,
        [(64, 64), (32, 32)],
        scale={"y": 0.65, "x": 0.65},
        translation={"y": 10.0, "x": 20.0},
    )
    assert physical_size(root) == []
    assert levels_aligned(root, sample=1) == []


def test_a_level_one_pixel_wider_than_its_base_divides_into_passes():
    root = _collection(
        "0.6",
        [(40, 40), (6, 6)],
        scale={"y": 0.65, "x": 0.65},
        level_scales=[{"y": 0.65, "x": 0.65}, {"y": 5.2, "x": 5.2}],
    )
    assert [p for p in levels_aligned(root, sample=1) if "scale" in p] == []


def test_a_level_with_the_wrong_scale_fails():
    root = _collection(
        "0.6",
        [(40, 40), (5, 5)],
        scale={"y": 0.65, "x": 0.65},
        level_scales=[{"y": 0.65, "x": 0.65}, {"y": 2.6, "x": 2.6}],
    )
    assert any("scale 2.6" in p for p in levels_aligned(root, sample=1))


def test_a_study_without_a_collection_declares_the_images_it_holds():
    """CAMELYON17's layout: unrelated slides in patient groups, with labels and no collection or scene."""
    from minus80.ome.verify import series_matches

    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    for path in ("patient_1/node_0/image", "patient_1/node_1/image", "patient_1/node_1/macro"):
        image = root.require_group(path)
        image.attrs["ome"] = image_attrs(name=path, dims=("y", "x"), level_shapes=[(64, 64), (32, 32)],
                                         scale={"y": 0.5, "x": 0.5}, units=UNITS, version="0.6")
        for i, shape in enumerate([(64, 64), (32, 32)]):
            image.create_array(str(i), data=np.indices(shape).sum(0).astype("u2"), dimension_names=("y", "x"))
    label = root.require_group("patient_1/node_0/image/labels/mask")
    label.attrs["ome"] = dict(root["patient_1/node_0/image"].attrs)["ome"]
    assert list(images(root)) == ["patient_1/node_0/image", "patient_1/node_1/image", "patient_1/node_1/macro"]
    assert series_matches(root) == []
    assert levels_aligned(root, sample=3) == [] and physical_size(root) == []


def _cropped(level_1_width):
    """A two-level image whose level 1 is cropped to ``level_1_width`` and keeps a 2x pixel size."""
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.attrs.update(root_attrs("0.6"))
    root.create_group("OME").attrs.update(series_attrs(["img"], version="0.6"))
    image = root.create_group("img")
    shapes = [(64, 1100), (32, level_1_width)]
    image.attrs["ome"] = image_attrs(name="img", dims=("y", "x"), level_shapes=shapes, units=UNITS,
                                     scale={"y": 0.25, "x": 0.25},
                                     level_scales=[{"y": 0.25, "x": 0.25}, {"y": 0.5, "x": 0.5}],
                                     version="0.6")
    for i, shape in enumerate(shapes):
        image.create_array(str(i), data=np.indices(shape).sum(0).astype("u2") // (i + 1),
                           chunks=(32, 256), dimension_names=("y", "x"))
    return root


def test_a_level_cropped_to_whole_chunks_passes():
    assert [p for p in levels_aligned(_cropped(512), sample=1) if "scale" in p] == []


def test_a_level_cropped_off_a_chunk_boundary_fails():
    assert any("scale 0.5" in p for p in levels_aligned(_cropped(520), sample=1))
