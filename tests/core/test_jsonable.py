import enum
import json

import numpy as np

from minus80.core.jsonable import jsonable


class Colour(enum.IntEnum):
    RED = 1


def test_non_finite_floats_become_null_at_any_depth():
    got = jsonable({"a": [float("nan"), 1.5], "b": {"c": float("inf"), "d": -np.inf}})
    assert got == {"a": [None, 1.5], "b": {"c": None, "d": None}}


def test_numpy_scalars_and_arrays_become_python():
    got = jsonable({"i": np.int64(3), "f": np.float32(0.5), "b": np.bool_(True),
                    "a": np.array([[1, 2], [3, 4]], dtype=np.uint16)})
    assert got == {"i": 3, "f": 0.5, "b": True, "a": [[1, 2], [3, 4]]}
    assert type(got["i"]) is int and type(got["b"]) is bool


def test_utf8_bytes_become_text():
    assert jsonable(b"caf\xc3\xa9") == "café"


def test_other_bytes_become_base64():
    assert jsonable(b"\xff\xfe") == {"base64": "//4="}


def test_enum_becomes_its_name():
    assert jsonable({"colour": Colour.RED}) == {"colour": "RED"}


def test_keys_become_strings():
    assert jsonable({1: "a", (2, 3): "b"}) == {"1": "a", "(2, 3)": "b"}


def test_object_with_no_json_form_becomes_its_str():
    class Ref:
        def __str__(self):
            return "<HDF5 object reference>"

    assert jsonable([Ref()]) == ["<HDF5 object reference>"]


def test_output_is_strict_json():
    value = {"nan": np.float64("nan"), "bytes": b"\x00\xff", "arr": np.arange(3), 4: (1, None)}
    json.dumps(jsonable(value), allow_nan=False)
