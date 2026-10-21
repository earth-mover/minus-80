import icechunk
import numpy as np
import zarr

import virtual_ome as vo
from virtual_ome.verify import GATES, provenance

SLIDE = "s3://bucket/slides/slide.tif"
MASK = "s3://bucket/masks/slide_mask.tif"


def _store(image_attrs: dict, level_attrs: dict | None = None, *, mask_url=MASK) -> zarr.Group:
    """A one-image collection whose level 0 references SLIDE and whose label references ``mask_url``.

    The label's origin chunk is left empty, as a mask's blank corner tile is,
    and the label's second level is native.
    """
    session = icechunk.Repository.create(icechunk.in_memory_storage()).writable_session("main")
    root = zarr.group(session.store, attributes=vo.root_attrs())
    root.create_group("OME", attributes=vo.series_attrs(["slide"]))
    image = root.create_group("slide", attributes={
        "ome": vo.image_attrs(name="slide", dims=("y", "x"), level_shapes=[(4, 4)],
                              scale={"y": 0.25, "x": 0.25},
                              units={"y": "micrometer", "x": "micrometer"}),
        **image_attrs})
    image.create_array("0", shape=(4, 4), chunks=(2, 2), dtype="uint8",
                       dimension_names=("y", "x"), attributes=level_attrs or {})
    for i in range(2):
        for j in range(2):
            session.store.set_virtual_ref(f"slide/0/c/{i}/{j}", SLIDE, offset=4 * (2 * i + j), length=4,
                                          validate_container=False)
    mask = image.create_group("labels").create_group("mask", attributes={"source_url": mask_url})
    mask.create_array("0", shape=(4, 4), chunks=(2, 2), dtype="uint8", dimension_names=("y", "x"))
    session.store.set_virtual_ref("slide/labels/mask/0/c/1/1", MASK, offset=0, length=4,
                                  validate_container=False)
    mask.create_array("1", shape=(2, 2), chunks=(2, 2), dtype="uint8", dimension_names=("y", "x"))[:] = \
        np.ones((2, 2), "uint8")
    return root


def test_passes_with_source_on_group_or_annotations():
    assert provenance(_store({"source_url": SLIDE})) == []
    assert provenance(_store(vo.image_annotation({}, source_url=SLIDE))) == []


def test_passes_with_source_on_array_as_list():
    assert provenance(_store({}, {"source_url": [SLIDE]})) == []


def test_missing_source_url():
    (problem,) = provenance(_store({}))
    assert problem.startswith("slide/0: virtual, but")


def test_source_url_not_among_references():
    (problem,) = provenance(_store({"source_url": SLIDE}, mask_url="s3://bucket/masks/other.tif"))
    assert problem.startswith("slide/labels/mask/0: source_url s3://bucket/masks/other.tif is not")


def test_not_icechunk():
    root = zarr.group(zarr.storage.MemoryStore())
    (problem,) = provenance(root)
    assert "not Icechunk" in problem


def test_registered():
    assert "provenance" in GATES
