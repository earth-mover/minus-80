import pytest

from minus80.ome.plate import plate_attrs, plate_grid, well_attrs


def test_96_well_grid():
    rows, columns = plate_grid(96)
    assert rows == list("ABCDEFGH")
    assert columns == [str(c) for c in range(1, 13)]


def test_1536_well_grid_runs_past_z():
    rows, _ = plate_grid(1536)
    assert rows[25:27] == ["Z", "AA"]
    assert len(rows) == 32


def test_sparse_plate_declares_every_row_and_column():
    rows, columns = plate_grid(96)
    attrs = plate_attrs(name="PML0116", rows=rows, columns=columns,
                        wells=[("B", "12"), ("H", "1")], field_count=4)
    plate = attrs["ome"]["plate"]
    assert attrs["ome"]["version"] == "0.5"
    assert len(plate["rows"]) == 8 and len(plate["columns"]) == 12
    assert plate["wells"] == [{"path": "B/12", "rowIndex": 1, "columnIndex": 11},
                              {"path": "H/1", "rowIndex": 7, "columnIndex": 0}]
    assert plate["field_count"] == 4
    assert "version" not in plate


def test_well_outside_the_grid_raises():
    rows, columns = plate_grid(96)
    with pytest.raises(ValueError, match="outside"):
        plate_attrs(name="p", rows=rows, columns=columns, wells=[("I", "1")])


def test_well_lists_its_fields():
    attrs = well_attrs(["0", "1"])
    assert attrs == {"ome": {"version": "0.5", "well": {"images": [{"path": "0"}, {"path": "1"}]}}}


def test_well_acquisitions_must_match_images():
    assert well_attrs(["0"], acquisitions=[3])["ome"]["well"]["images"] == [
        {"path": "0", "acquisition": 3}]
    with pytest.raises(ValueError, match="acquisition ids"):
        well_attrs(["0", "1"], acquisitions=[0])
