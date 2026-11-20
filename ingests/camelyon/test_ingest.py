"""The ingest run end to end on the small copy of the bucket that fixture.py writes.

    uv run --package camelyon-ingest pytest --import-mode=prepend ingests/camelyon
"""

import json

import fixture
import ingest
import numpy as np
import pytest
import tifffile
import verify
import zarr
from minus80.ome import root_attrs, series_attrs
from minus80.ome.hierarchy import images
from minus80.core.documents import read_text
from minus80.core.icechunk_io import open_repository
from minus80.parsers import asap
from plumbing import Source


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = fixture.build(tmp_path_factory.mktemp("bucket"))
    return Source(root.as_uri() + "/"), root


@pytest.fixture(scope="module")
def repo(source, tmp_path_factory):
    src, _ = source
    location = str(tmp_path_factory.mktemp("repo") / "camelyon")
    ingest.run(location, ingest.C16, None, src, threads=2, replace=set())
    return location


def store(location, branch="main"):
    return zarr.open_group(open_repository(location).readonly_session(branch).store, mode="r")


@pytest.fixture(scope="module")
def both(repo, source):
    src, _ = source
    ingest.run(repo, ingest.C17, None, src, threads=4, replace=set(), batch=3)
    return store(repo)


def test_camelyon16_alone_describes_only_camelyon16(repo):
    root = store(repo)
    assert dict(root.attrs)["title"] == "CAMELYON16 lymph node metastases, virtualized"
    assert set(dict(root["CAMELYON16"].attrs)["annotations"]["slide"]) == set(fixture.SLIDES_16)


@pytest.mark.parametrize("challenge, slide", [("CAMELYON16", "tumor_001"), ("CAMELYON16", "normal_001"),
                                              ("CAMELYON17", "patient_080_node_0")])
def test_every_level_is_what_tifffile_decodes(both, source, challenge, slide):
    _, root_dir = source
    image = both[f"{ingest.slide_path(ingest.CHALLENGES[challenge], slide)}/image"]
    with tifffile.TiffFile(root_dir / challenge / "images" / f"{slide}.tif") as tif:
        for k, level in enumerate(tif.series[0].levels):
            # tifffile reads a cropped Philips level's length as level 0 halved,
            # past the page's tiles; the store keeps the page's own rows.
            stored = image[str(k)][:]
            want = np.moveaxis(level.asarray(), -1, 0)[:, : stored.shape[1], : stored.shape[2]]
            np.testing.assert_array_equal(stored, want)
        for extra in tif.series[1:]:
            np.testing.assert_array_equal(both[f"{image.path.rsplit('/', 1)[0]}/{extra.name.lower()}/0"][:],
                                          np.moveaxis(extra.asarray(), -1, 0))


def scales(group):
    ome = dict(group.attrs)["ome"]
    datasets = ome["multiscales"][0]["datasets"]
    out = []
    for d in datasets:
        transform = d["coordinateTransformations"][0]
        if transform["type"] == "sequence":
            transform = transform["transformations"][0]
        out.append(transform["scale"][-1])
    return out


def test_a_philips_level_is_2_to_the_k_coarser_even_where_it_is_cropped(both):
    image = both["CAMELYON17/patient_080/node_0/image"]
    assert image[str(fixture.CROPPED)].shape[1:] == (128, 256)
    assert scales(image) == pytest.approx([fixture.PHILIPS_SCANNED_UM * 2.0 ** k for k in range(fixture.LEVELS)],
                                          rel=1e-12)
    attrs = dict(image.attrs)
    assert f"Levels {fixture.CROPPED} are narrower" in attrs["level_shapes_note"]
    assert attrs["pixel_size"]["um"] == pytest.approx([fixture.PHILIPS_SCANNED_UM] * 2, rel=1e-12)


def test_a_generic_level_scale_follows_its_shape(both):
    image = both["CAMELYON16/normal_001/image"]
    shapes = [image[str(k)].shape[2] for k in range(fixture.LEVELS)]
    assert scales(image) == pytest.approx([fixture.GENERIC_UM * shapes[0] / s for s in shapes], rel=1e-12)


def test_mask_levels_the_file_lacks_are_derived_on_the_image_grid(both):
    mask = both["CAMELYON17/patient_080/node_0/image/labels/mask"]
    attrs = dict(mask.attrs)
    last = str(fixture.LEVELS - 1)
    assert list(attrs["derived_levels"]) == [last]
    assert set(np.unique(mask[last][:])) <= {0, 1, 2}
    assert mask[str(fixture.CROPPED)].shape == (200, 275)
    assert "grid_note" in attrs
    assert scales(mask) == scales(both["CAMELYON17/patient_080/node_0/image"])


def test_the_negative_mask_missing_from_checksums_says_so(both):
    attrs = dict(both["CAMELYON17/patient_000/node_1/image/labels/mask"].attrs)
    assert "source_md5" not in attrs and "does not list" in attrs["source_md5_note"]


def test_a_tissue_file_copied_from_the_mask_is_left_out(both):
    row = dict(both["CAMELYON16/normal_001"].attrs)["annotations"]
    assert row["has_tissue"] is False and "copy of the mask" in row["tissue_note"]
    assert "tissue" not in both["CAMELYON16/normal_001"]
    assert dict(both["CAMELYON16/tumor_001/tissue"].attrs)["matches_image_level"] == 3


def test_outlines_rebuild_the_xml_and_cut_the_hole(both):
    shapes = both["CAMELYON17/patient_080/node_0/shapes"]
    table = {k: shapes[k][:] for k in ("coords", "offset0", *asap.COLUMNS)}
    table["groups"] = dict(shapes.attrs)["asap_groups"]
    assert asap.to_xml(table) == fixture.asap_xml()
    features = json.loads(bytes(shapes["geojson"][:]))["features"]
    assert len(features) == 1 and len(features[0]["geometry"]["coordinates"]) == 2


def test_camelyon17_rows_come_from_stages_csv(both):
    rows = {r: dict(both[ingest.slide_path(ingest.C17, r)].attrs)["annotations"] for r in fixture.SLIDES_17}
    assert (rows["patient_080_node_0"]["stage"], rows["patient_080_node_0"]["center"]) == ("micro", "4")
    assert rows["patient_080_node_0"]["scanner"] == "3D Histech"
    test = rows["patient_100_node_0"]
    assert (test["split"], test["stage"], test["center"]) == ("test", None, None)
    assert "replaced" in test["note"]
    assert rows["patient_000_node_0"]["has_mask"] is False
    patient = dict(both["CAMELYON17/patient_080"].attrs)["annotations"]
    assert (patient["stage"], patient["center"], patient["split"]) == ("pN1mi", "4", "training")


def test_the_container_and_root_describe_both_challenges(both):
    attrs = dict(both["CAMELYON17"].attrs)
    assert "ome" not in attrs and "OME" not in both["CAMELYON17"]
    assert dict(both["CAMELYON17/patient_080/node_0/image"].attrs)["ome"]["version"] == "0.6"
    assert "patient_080/node_0/image" in attrs["annotations"]["image"]
    assert attrs["patients"]["group"] == ["patient_000", "patient_080", "patient_100"]
    assert attrs["center_names"]["4"].startswith("Laboratorium")
    assert "Table 4" in attrs["center_names_decision"]
    assert read_text(both, attrs["source_files"]["stages.csv"]).startswith("patient,stage,center")
    assert set(images(both["CAMELYON17"])) == {f"{ingest.slide_path(ingest.C17, s).split('/', 1)[1]}/image"
                                               for s in fixture.SLIDES_17} | {"patient_080/node_0/macro"}
    root = dict(both.attrs)
    assert root["title"].startswith("CAMELYON16 and CAMELYON17")
    assert isinstance(root["citation"], str) and "doi:10.1109/TMI.2018.2867350" in root["citation"]
    assert set(root["citations"]) == {"CAMELYON16", "CAMELYON17", "dataset"}
    assert dict(both["CAMELYON16"].attrs)["ome"]["version"] == "0.5"


def test_camelyon17_is_committed_in_batches_each_with_a_whole_table(both, repo):
    messages = [s.message for s in open_repository(repo).ancestry(branch="main")]
    batches = [m for m in messages if m.startswith("CAMELYON17: add")]
    assert batches == ["CAMELYON17: add 1 slide (patient_100_node_0 to patient_100_node_0); the container now holds 4 slides",
                       "CAMELYON17: add 3 slides (patient_000_node_0 to patient_080_node_0); the container now holds 3 slides"]


def test_verify_passes(both, repo, source):
    src, _ = source
    for challenge in ("CAMELYON16", "CAMELYON17"):
        with pytest.raises(SystemExit) as exit_:
            verify.main(["--repo", repo, "--challenge", challenge, "--source", src.root, "--pixels", "4"])
        assert exit_.value.code is False


def test_a_rerun_adds_nothing_and_a_replace_rebuilds_one_slide(both, repo, source):
    src, _ = source
    before = open_repository(repo).lookup_branch("main")
    assert ingest.run(repo, ingest.C17, None, src, threads=2, replace=set()) is None
    assert open_repository(repo).lookup_branch("main") == before
    ingest.run(repo, ingest.C17, ["patient_000_node_0"], src, threads=1, replace={"patient_000_node_0"})
    message = next(iter(open_repository(repo).ancestry(branch="main"))).message
    assert message.startswith("CAMELYON17: rebuild patient_000_node_0")
    assert set(dict(store(repo)["CAMELYON17"].attrs)["annotations"]["slide"]) == set(fixture.SLIDES_17)


def test_drop_bioformats2raw_removes_the_layout_once(both, repo):
    import drop_bioformats2raw
    from minus80.core.icechunk_io import commit

    session = open_repository(repo).writable_session("main")
    root = zarr.open_group(session.store, mode="a")
    root["CAMELYON17"].attrs.update(root_attrs("0.6"))
    root.require_group("CAMELYON17/OME").attrs.update(series_attrs(["patient_000/node_0/image"], "0.6"))
    commit(session, "write the layout an earlier ingest wrote")
    c16_before = dict(store(repo)["CAMELYON16"].attrs)

    assert drop_bioformats2raw.run(repo) is not None
    after = store(repo)
    assert "ome" not in dict(after["CAMELYON17"].attrs) and "OME" not in after["CAMELYON17"]
    assert dict(after["CAMELYON16"].attrs) == c16_before and "OME" in after["CAMELYON16"]
    assert next(iter(open_repository(repo).ancestry(branch="main"))).message == drop_bioformats2raw.MESSAGE
    assert drop_bioformats2raw.run(repo) is None
