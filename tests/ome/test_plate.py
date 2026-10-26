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


def test_verify_finds_plate_fields():
    import zarr

    from minus80.ome import image_attrs
    from minus80.ome.hierarchy import images
    from minus80.ome.verify import schema, series_matches

    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    rows, columns = plate_grid(96)
    plate = root.create_group("stacks/P1")
    plate.attrs.update(plate_attrs(name="P1", rows=rows, columns=columns, wells=[("B", "12")]))
    plate.create_group("B/12").attrs.update(well_attrs(["F1", "F2"]))
    for field in ("F1", "F2"):
        plate.create_group(f"B/12/{field}").attrs.update({"ome": image_attrs(
            name=field, dims=("y", "x"), level_shapes=[(4, 4)], scale={"y": 1, "x": 1})})
    assert sorted(images(root)) == ["stacks/P1/B/12/F1", "stacks/P1/B/12/F2"]
    assert series_matches(root) == []
    assert schema(root, "0.5") == []
    root.create_group("stacks/P1/B/12/F3").attrs.update(dict(root["stacks/P1/B/12/F1"].attrs))
    assert series_matches(root) == ["1 groups missing from series and plates: ['stacks/P1/B/12/F3']"]


def test_schema_gate_checks_plate_documents():
    import zarr

    from minus80.ome.verify import schema

    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    root.create_group("P1").attrs.update({"ome": {"version": "0.5", "plate": {"rows": []}}})
    assert [p.split(":")[0] for p in schema(root, "0.5")] == ["P1"]


def test_verify_finds_derived_images_and_nested_collections():
    import zarr

    from minus80.ome import image_attrs, root_attrs, series_attrs
    from minus80.ome.hierarchy import DERIVED, images
    from minus80.ome.verify import series_matches

    def image(group, dims, shape):
        group.attrs.update({"ome": image_attrs(name=group.name, dims=dims, level_shapes=[shape],
                                               scale={d: 1 for d in dims})})

    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    rows, columns = plate_grid(96)
    root.create_group("plates/P1").attrs.update(
        plate_attrs(name="P1", rows=rows, columns=columns, wells=[("A", "1")]))
    root.create_group("plates/P1/A/1").attrs.update(well_attrs(["F1"]))
    field = root.create_group("plates/P1/A/1/F1")
    image(field, ("z", "y", "x"), (3, 4, 4))
    image(field.create_group("proj"), ("y", "x"), (4, 4))
    collection = root.create_group("others")
    collection.attrs.update(root_attrs())
    collection.create_group("OME").attrs.update(series_attrs(["G/F2"]))
    image(collection.create_group("G/F2"), ("y", "x"), (4, 4))

    assert series_matches(root) == ["1 groups missing from series and plates: ['plates/P1/A/1/F1/proj']"]
    field.attrs.update({DERIVED: {"proj": "maximum over z"}})
    assert series_matches(root) == []
    assert sorted(images(root)) == ["others/G/F2", "plates/P1/A/1/F1", "plates/P1/A/1/F1/proj"]


def test_plate_at_the_root_is_found_and_validated():
    import zarr

    from minus80.ome import image_attrs
    from minus80.ome.hierarchy import images
    from minus80.ome.verify import schema, series_matches

    root = zarr.open_group(zarr.storage.MemoryStore(), mode="w")
    rows, columns = plate_grid(96)
    attrs = plate_attrs(name="P", rows=rows, columns=columns, wells=[("A", "1")])
    # bioformats2raw writes its layout key on a plate root too; the plate takes precedence.
    attrs["ome"]["bioformats2raw.layout"] = 3
    root.attrs.update(attrs)
    root.create_group("A/1").attrs.update(well_attrs(["0"]))
    root.create_group("A/1/0").attrs.update({"ome": image_attrs(
        name="f", dims=("y", "x"), level_shapes=[(4, 4)], scale={"y": 1, "x": 1})})
    assert list(images(root)) == ["A/1/0"]
    assert series_matches(root) == []
    assert schema(root, "0.5") == []
    root.attrs.update({"ome": {"version": "0.5", "plate": {"rows": []}}})
    assert [p.split(":")[0] for p in schema(root, "0.5")] == ["plate"]
