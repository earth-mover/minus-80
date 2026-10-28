import numpy as np
import pytest

from minus80.ome.crop import crop_levels

SCALES = [{"z": 1.0, "y": 0.65 * 2**k, "x": 0.65 * 2**k} for k in range(4)]


def _shapes(h, w, z=7):
    shapes = [(z, h, w)]
    for _ in range(3):
        z, h, w = shapes[-1]
        shapes.append((z, h // 2, w // 2))
    return shapes


def test_a_window_on_the_coarse_grid_halves_exactly():
    levels = crop_levels(
        {"z": 0, "y": 0, "x": 8000}, {"z": 7, "y": 1000, "x": 1000}, _shapes(2000, 10000), SCALES
    )
    assert [lvl.window["x"] for lvl in levels] == [
        (8000, 9000),
        (4000, 4500),
        (2000, 2250),
        (1000, 1125),
    ]
    assert levels[0].translation == {"z": 0.0, "y": 0.0, "x": 0.0}
    # A level-3 pixel spans 8 level-0 pixels, so its center is 3.5 of them in.
    assert levels[3].translation["x"] == pytest.approx(3.5 * 0.65)


def test_a_window_off_the_coarse_grid_covers_every_pixel_it_touches():
    # x 6655 is half way through level-1 pixel 3327.
    levels = crop_levels(
        {"z": 0, "y": 0, "x": 6655}, {"z": 7, "y": 1000, "x": 1000}, _shapes(2000, 10000), SCALES
    )
    assert levels[1].window["x"] == (3327, 3828)
    assert levels[1].shape == (7, 500, 501)
    # Level-1 pixel 3327 is level-0 pixels 6654 and 6655, centered at 6654.5:
    # one half pixel before the window's origin.
    assert levels[1].translation["x"] == pytest.approx(-0.5 * 0.65)


def test_a_window_stops_where_a_rounded_down_level_ends():
    # 90 rows: level 2 holds 22, but a field ending on row 90 touches row 22.
    levels = crop_levels(
        {"z": 0, "y": 50, "x": 0}, {"z": 7, "y": 40, "x": 40}, _shapes(90, 130), SCALES
    )
    assert levels[2].window["y"] == (12, 22)


def test_the_window_reads_the_pixels_it_says():
    rng = np.random.default_rng(0)
    level1 = rng.integers(0, 100, (7, 1000, 5000))
    (_, lvl) = crop_levels(
        {"z": 0, "y": 0, "x": 6655},
        {"z": 7, "y": 1000, "x": 1000},
        _shapes(2000, 10000)[:2],
        SCALES[:2],
    )
    np.testing.assert_array_equal(level1[lvl.slices(("z", "y", "x"))], level1[:, 0:500, 3327:3828])


def test_a_factor_that_is_not_whole_raises():
    with pytest.raises(ValueError, match="not a whole number"):
        crop_levels(
            {"y": 0, "x": 0},
            {"y": 10, "x": 10},
            [(10, 10), (7, 7)],
            [{"y": 1.0, "x": 1.0}, {"y": 1.5, "x": 1.5}],
        )
