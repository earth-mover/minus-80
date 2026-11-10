import numpy as np
import zarr

from minus80.ome import image_attrs, verify
from minus80.ome.plate import plate_attrs, plate_grid, well_attrs

FACTOR = 4
SHAPES = [(32, 48), (8, 12)]
FACTORS = [{"y": 1, "x": 1}, {"y": FACTOR, "x": FACTOR}]


def _store(shifts):
    """One plate with a field per entry of ``shifts``: its level 1's translation, in um."""
    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    rows, columns = plate_grid(96)
    plate = root.create_group("P1")
    plate.attrs.update(plate_attrs(name="P1", rows=rows, columns=columns, wells=[("A", "1")]))
    plate.create_group("A/1").attrs.update(well_attrs([f"F{i}" for i in range(len(shifts))]))
    data = np.random.default_rng(0).random(SHAPES[0])
    coarse = data.reshape(8, FACTOR, 12, FACTOR).mean((1, 3))
    for i, shift in enumerate(shifts):
        field = plate.create_group(f"A/1/F{i}")
        attrs = image_attrs(name=f"F{i}", dims=("y", "x"), level_shapes=SHAPES,
                            scale={"y": 1.0, "x": 1.0}, downsampling="mean",
                            level_factors=FACTORS, translation={"y": 0.0, "x": 0.0})
        datasets = attrs["multiscales"][0]["datasets"]
        datasets[1]["coordinateTransformations"][1]["translation"] = [shift, shift]
        field.attrs.update({"ome": attrs})
        for level, values in enumerate((data, coarse)):
            field.create_array(str(level), shape=values.shape, dtype=values.dtype,
                               dimension_names=("y", "x"))[:] = values
    return root


def test_correctly_placed_level_passes():
    assert verify.levels_aligned(_store([(FACTOR - 1) / 2]), 5) == []


def test_level_shifted_by_half_a_block_fails():
    problems = verify.levels_aligned(_store([(FACTOR - 1) / 2 + FACTOR / 2]), 5)
    assert problems and all("outer edge" in p for p in problems)


def test_images_without_translations_are_not_placed():
    root = _store([0.0])
    attrs = dict(root["P1/A/1/F0"].attrs)
    for d in attrs["ome"]["multiscales"][0]["datasets"]:
        d["coordinateTransformations"] = d["coordinateTransformations"][:1]
    root["P1/A/1/F0"].attrs.update(attrs)
    assert verify.levels_aligned(root, 5) == []


def test_sample_spreads_across_the_store():
    # Only the last field is misplaced, so a check of the first image would miss it.
    root = _store([1.5, 1.5, 1.5, 1.5, 3.0])
    assert verify.levels_aligned(root, 2) != []
    assert verify.levels_aligned(root, 1) == []
    assert verify._spread(list("abcdefghij"), 4) == ["a", "d", "g", "j"]
    assert verify._spread(list("abc"), 10) == ["a", "b", "c"]
