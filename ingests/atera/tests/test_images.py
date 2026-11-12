"""The full image elements, written from a synthetic bundle and a synthetic H&E object."""

import io
import json
import os
import sys
import zipfile
from contextlib import contextmanager

import numpy as np
import pytest
import tifffile
import zarr
from tiff_pyramids import picture, pyramid_tiff

import ingest
from bundle import Bundle, read_alignment
from ingest import write_he, write_morphology, write_morphology_3d
from minus80.core import pyramid
from minus80.core.elements import ElementWriter
from minus80.core.icechunk_io import create_repo
from minus80.core.remote import ReadCounter

PIXEL = 0.2125
Z_STEP = 2.5
SHAPE, TILE, LEVELS, PLANES = (1300, 2100), (1024, 1024), 3, 3
XML = """<?xml version="1.0"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">
  <Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16" PhysicalSizeX="0.2125" PhysicalSizeY="0.2125">
{channels}
  </Pixels></Image>
</OME>"""
STAINS = ["DAPI", "ATP1A1/CD45/E-Cadherin", "18S", "alphaSMA/Vimentin"]
MORPHOLOGY_2D = [f"morphology_2d/ch000{i}_{s}.ome.tif" for i, s in enumerate(("dapi", "atp1a1", "18s", "asma"))]
MORPHOLOGY_3D = "morphology_3d/ch0000_dapi_3d.ome.tif"
ALIGNMENT = np.array([[0.0, -1.289, 40.0], [1.289, 0.0, 12.5], [0.0, 0.0, 1.0]])


def _xml(names):
    channels = "\n".join(f'    <Channel ID="Channel:0:{i}" Name="{n}"/>' for i, n in enumerate(names))
    return XML.format(channels=channels)


def _level_shapes(shape=SHAPE, levels=LEVELS):
    return [tuple(-(-n // 2**k) for n in shape) for k in range(levels)]


def _rgb_pyramid(path, *, planar: bool):
    """An RGB pyramid of the H&E's two layouts: interleaved Deflate, or planar and uncompressed."""
    args = dict(photometric="rgb", tile=TILE)
    args |= dict(planarconfig="separate") if planar else dict(planarconfig="contig", compression="adobe_deflate")
    data = picture((*SHAPE, 3), np.uint8)
    with tifffile.TiffWriter(path, ome=False) as tw:
        tw.write(np.moveaxis(data, -1, 0) if planar else data, subifds=LEVELS - 1, **args)
        for _ in range(LEVELS - 1):
            data = data[::2, ::2]
            tw.write(np.moveaxis(data, -1, 0) if planar else data, subfiletype=1, **args)


def image_members(tmp_path) -> dict:
    """The members of a bundle that the image elements read: four pyramids, a stack and ``experiment.spatial``."""
    members = {}
    for channel, name in enumerate(MORPHOLOGY_2D):
        path = tmp_path / f"c{channel}.tif"
        pyramid_tiff(path, 1, SHAPE, LEVELS, TILE, _xml(STAINS))
        members[name] = path.read_bytes()
    stack = {"PhysicalSizeX": PIXEL, "PhysicalSizeY": PIXEL, "PhysicalSizeZ": Z_STEP, "Channel": {"Name": ["DAPI"]}}
    pyramid_tiff(tmp_path / "3d.tif", PLANES, SHAPE, LEVELS, TILE, metadata=stack)
    members[MORPHOLOGY_3D] = (tmp_path / "3d.tif").read_bytes()
    members["experiment.spatial"] = json.dumps({"pixel_size": PIXEL}).encode()
    return members


def write_bundle(path, members) -> None:
    """Write ``members`` as an outs.zip with stored members."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)


@pytest.fixture
def bundle(tmp_path):
    """A zip of four one-channel pyramids, a three-plane stack and ``experiment.spatial``."""
    members = image_members(tmp_path)
    cells = io.BytesIO()
    with zipfile.ZipFile(cells, "w") as zf:
        zf.writestr(".zattrs", json.dumps({"segmentation_methods": ["m"]}))
    members["cells.zarr.zip"] = cells.getvalue()
    write_bundle(tmp_path / "outs.zip", members)
    return Bundle(tmp_path / "outs.zip")


def _repo(directory):
    repo = create_repo(directory, [])
    repo.create_branch("work", repo.lookup_branch("main"))
    return repo


def _tree(repo, branch="work"):
    """Every node of the branch with its attributes, and each array's shape, dtype and bytes."""
    root = zarr.open_group(repo.readonly_session(branch=branch).store, mode="r")
    tree = {"": dict(root.attrs)}
    for path, node in root.members(max_depth=None):
        tree[path] = (dict(node.attrs), node.shape, str(node.dtype), node[:].tobytes()) if isinstance(
            node, zarr.Array) else dict(node.attrs)
    return tree


class CheckedWriter(ElementWriter):
    """Checks after every commit that each image group lists exactly the levels it holds."""

    checks = 0

    @contextmanager
    def unit(self, path, message, **kwargs):
        with super().unit(path, message, **kwargs) as pair:
            yield pair
        root = zarr.open_group(self.repo.readonly_session(branch=self.branch).store, mode="r")
        for name, group in root["images"].groups():
            listed = [d["path"] for d in group.attrs["ome"]["multiscales"][0]["datasets"]]
            assert listed == sorted(n for n, _ in group.arrays()), f"images/{name} after {message}"
            self.checks += 1


@pytest.fixture
def writer(tmp_path):
    return CheckedWriter(_repo(tmp_path / "repo"), "work", counter=ReadCounter())


def test_full_morphology_writes_every_level_and_marks_it_complete(bundle, writer):
    write_morphology(writer, bundle, PIXEL, concurrency=4)
    root = zarr.open_group(writer.repo.readonly_session(branch="work").store, mode="r")
    shapes = _level_shapes()
    assert sorted(n for n, _ in root["images/morphology"].arrays()) == ["s0", "s1", "s2"]
    assert writer.complete("images/morphology")
    assert writer.checks == LEVELS
    for k, shape in enumerate(shapes):
        array = root[f"images/morphology/s{k}"]
        assert array.shape == (4, *shape) and array.chunks == (1, *TILE)
        for channel, name in enumerate(MORPHOLOGY_2D):
            expected = bundle.tiff(name).series[0].levels[k].pages[0].asarray()
            np.testing.assert_array_equal(array[channel], expected)
    ome = root["images/morphology"].attrs["ome"]
    assert [c["label"] for c in ome["omero"]["channels"]] == STAINS
    transform = ome["multiscales"][0]["coordinateTransformations"][0]
    assert transform["scale"] == [1.0, PIXEL, PIXEL]


def test_a_rerun_after_a_stop_skips_finished_levels_and_equals_one_run(bundle, writer, tmp_path, monkeypatch):
    class Stops(CheckedWriter):
        @contextmanager
        def unit(self, path, message, **kwargs):
            if path.endswith("/s1"):
                raise KeyboardInterrupt
            with super().unit(path, message, **kwargs) as pair:
                yield pair

    with pytest.raises(KeyboardInterrupt):
        write_morphology(Stops(writer.repo, "work"), bundle, PIXEL, concurrency=2)
    assert writer.has("images/morphology/s0") and not writer.has("images/morphology/s1")
    assert not writer.complete("images/morphology")

    decoded = []
    real = pyramid._crop
    monkeypatch.setattr(pyramid, "_crop", lambda page, *a: decoded.append(page.imagelength) or real(page, *a))
    write_morphology(writer, bundle, PIXEL, concurrency=2)
    assert SHAPE[0] not in decoded and set(decoded) == {_level_shapes()[1][0], _level_shapes()[2][0]}
    assert writer.complete("images/morphology")

    whole = CheckedWriter(_repo(tmp_path / "whole"), "work")
    write_morphology(whole, bundle, PIXEL, concurrency=2)
    assert _tree(writer.repo) == _tree(whole.repo)


def test_the_fixture_level_is_written_alone_and_not_marked_complete(bundle, writer):
    write_morphology(writer, bundle, PIXEL, levels=[1], concurrency=1)
    root = zarr.open_group(writer.repo.readonly_session(branch="work").store, mode="r")
    assert sorted(n for n, _ in root["images/morphology"].arrays()) == ["s1"]
    assert not writer.complete("images/morphology")
    ome = root["images/morphology"].attrs["ome"]
    assert [d["path"] for d in ome["multiscales"][0]["datasets"]] == ["s1"]
    # One level's pixel is the full-resolution pixel times full size over level size.
    scale = ome["multiscales"][0]["coordinateTransformations"][0]["scale"]
    assert scale == pytest.approx([1.0, PIXEL * SHAPE[0] / _level_shapes()[1][0], PIXEL * SHAPE[1] / _level_shapes()[1][1]])


def test_the_stack_is_written_one_shard_per_tile_and_every_plane_matches(bundle, writer, monkeypatch):
    sets = {}
    real = zarr.Array.__setitem__
    monkeypatch.setattr(zarr.Array, "__setitem__", lambda a, k, v: sets.__setitem__(
        (a.path, str(k[-2:])), sets.get((a.path, str(k[-2:])), 0) + 1) or real(a, k, v))
    write_morphology_3d(writer, bundle, PIXEL, concurrency=3)
    assert set(sets.values()) == {1}
    root = zarr.open_group(writer.repo.readonly_session(branch="work").store, mode="r")
    assert sorted(n for n, _ in root["images/morphology_3d"].arrays()) == ["s0", "s1", "s2"]
    assert writer.complete("images/morphology_3d") and writer.checks == LEVELS
    for k, shape in enumerate(_level_shapes()):
        array = root[f"images/morphology_3d/s{k}"]
        assert array.shape == (1, PLANES, *shape)
        assert array.shards == (1, PLANES, *TILE) and array.chunks == (1, 1, *TILE)
        pages = bundle.tiff(MORPHOLOGY_3D).series[0].levels[k].pages
        assert len(pages) == PLANES
        for z, page in enumerate(pages):
            np.testing.assert_array_equal(array[0, z], page.asarray())
    ome = root["images/morphology_3d"].attrs["ome"]
    assert [a["name"] for a in ome["multiscales"][0]["axes"]] == ["c", "z", "y", "x"]
    assert ome["multiscales"][0]["coordinateTransformations"][0]["scale"] == [1.0, Z_STEP, PIXEL, PIXEL]


def test_the_stack_decodes_with_one_thread_for_each_plane_up_to_the_cpu_count(bundle, writer, monkeypatch):
    decoders = []
    real = ingest.write_stack
    monkeypatch.setattr(ingest, "write_stack", lambda *a, **kw: decoders.append(kw.get("decoders")) or real(*a, **kw))
    write_morphology_3d(writer, bundle, PIXEL, levels=[0], concurrency=2)
    assert decoders == [min(PLANES, os.cpu_count() or 1)]
    root = zarr.open_group(writer.repo.readonly_session(branch="work").store, mode="r")
    page = bundle.tiff(MORPHOLOGY_3D).series[0].levels[0].pages[1]
    np.testing.assert_array_equal(root["images/morphology_3d/s0"][0, 1], page.asarray())


def test_a_rerun_of_the_stack_decodes_no_held_level(bundle, writer, monkeypatch):
    write_morphology_3d(writer, bundle, PIXEL, levels=[0], concurrency=1)
    decoded = []
    real = pyramid._decode_segments
    monkeypatch.setattr(pyramid, "_decode_segments", lambda page, *a: decoded.append(page.imagelength) or real(page, *a))
    write_morphology_3d(writer, bundle, PIXEL, concurrency=1)
    assert SHAPE[0] not in decoded and decoded
    assert writer.complete("images/morphology_3d")


@pytest.mark.parametrize("planar", [False, True], ids=["interleaved_deflate", "planar_uncompressed"])
def test_he_is_channel_first_with_the_alignment_transform(tmp_path, writer, planar):
    he, alignment = tmp_path / "he.ome.tif", tmp_path / "alignment.csv"
    _rgb_pyramid(he, planar=planar)
    np.savetxt(alignment, ALIGNMENT, delimiter=",")
    write_he(writer, str(he), str(alignment), PIXEL, concurrency=4)
    root = zarr.open_group(writer.repo.readonly_session(branch="work").store, mode="r")
    group = root["images/he_image"]
    assert sorted(n for n, _ in group.arrays()) == ["s0", "s1", "s2"]
    assert writer.complete("images/he_image") and writer.checks == LEVELS
    with tifffile.TiffFile(he, is_ome=False) as tif:
        for k, level in enumerate(tif.series[0].levels):
            array, data = group[f"s{k}"], level.asarray()
            assert array.dtype == np.uint8 and array.chunks == (1, *TILE)
            np.testing.assert_array_equal(array[:], data if planar else np.moveaxis(data, -1, 0))
    assert group["s0"].shape[0] == 3
    ome = group.attrs["ome"]
    assert [c["label"] for c in ome["omero"]["channels"]] == ["red", "green", "blue"]
    t = ome["multiscales"][0]["coordinateTransformations"][0]
    assert t["type"] == "sequence" and [s["type"] for s in t["transformations"]] == ["affine", "scale"]
    assert t["transformations"][0]["affine"] == ALIGNMENT[:-1].tolist()
    assert t["transformations"][0]["input"]["axes"][0]["name"] == "x"
    assert t["transformations"][1]["scale"] == [PIXEL, PIXEL]
    assert t["transformations"][0]["output"] == t["transformations"][1]["input"]
    assert group.attrs["alignment"] == {
        "matrix": ALIGNMENT.tolist(), "axes": ["x", "y"], "from": "he_image pixels", "to": "morphology pixels"}


def test_he_counts_its_bytes_in_the_writers_counter(tmp_path, writer):
    he, alignment = tmp_path / "he.ome.tif", tmp_path / "alignment.csv"
    _rgb_pyramid(he, planar=False)
    np.savetxt(alignment, ALIGNMENT, delimiter=",")
    write_he(writer, str(he), str(alignment), PIXEL, concurrency=1)
    assert writer.counter.bytes > 0


def test_a_malformed_alignment_is_refused(tmp_path, writer):
    he, alignment = tmp_path / "he.ome.tif", tmp_path / "alignment.csv"
    _rgb_pyramid(he, planar=False)
    alignment.write_text("1,2\n3,4\n")
    with pytest.raises(ValueError, match="3 by 3"):
        write_he(writer, str(he), str(alignment), PIXEL, concurrency=1)
    assert not writer.has("images/he_image")


def test_select_elements_accepts_a_subset_in_the_given_order_of_the_steps():
    available = ["tables/table", "images/morphology", "root"]
    assert ingest.select_elements("root,tables/table", available) == ["tables/table", "root"]
    assert ingest.select_elements(None, available) == available
    with pytest.raises(ValueError, match="images/he_image"):
        ingest.select_elements("images/he_image", available)


def _main(monkeypatch, tmp_path, *args):
    argv = ["ingest.py", "--sample", "kidney", "--repo", str(tmp_path / "repo"), "--catalog", str(tmp_path / "cat"),
            "--namespace", "n", *args]
    monkeypatch.setattr(sys, "argv", argv)
    ingest.main()


def test_full_mode_refuses_the_fixture_branch_and_fixture_mode_refuses_main(monkeypatch, tmp_path):
    def no_bundle(location):
        raise AssertionError("main() opened the bundle")

    monkeypatch.setattr(ingest, "open_bundle", no_bundle)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="fixture"):
        _main(monkeypatch, tmp_path, "--mode", "full", "--branch", "fixture")
    with pytest.raises(SystemExit, match="main"):
        _main(monkeypatch, tmp_path, "--mode", "fixture", "--branch", "main")


def test_full_mode_writes_the_image_elements_named_by_elements_from_local_objects(monkeypatch, tmp_path, bundle):
    he, alignment = tmp_path / "he.ome.tif", tmp_path / "alignment.csv"
    _rgb_pyramid(he, planar=False)
    np.savetxt(alignment, ALIGNMENT, delimiter=",")
    monkeypatch.chdir(tmp_path)
    _main(monkeypatch, tmp_path, "--mode", "full", "--branch", "full", "--bundle", str(tmp_path / "outs.zip"),
          "--he-image", str(he), "--he-alignment", str(alignment), "--concurrency", "2",
          "--elements", "images/morphology,images/morphology_3d,images/he_image")
    session = create_repo(tmp_path / "repo", [], open_existing=True).readonly_session(branch="full")
    root = zarr.open_group(session.store, mode="r")
    assert sorted(root["images"].group_keys()) == ["he_image", "morphology", "morphology_3d"]
    assert all(root[f"images/{n}"].attrs["minus80:complete"] for n in root["images"].group_keys())


def test_elements_with_rewrite_is_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="--elements"):
        _main(monkeypatch, tmp_path, "--branch", "work", "--rewrite", "points/transcripts", "--elements", "root")


def test_a_pixel_size_that_differs_from_the_files_is_refused(bundle, writer):
    with pytest.raises(ValueError, match="pixel size"):
        write_morphology(writer, bundle, 0.3, concurrency=1)
    with pytest.raises(ValueError, match="pixel size"):
        write_morphology_3d(writer, bundle, 0.3, concurrency=1)
    assert not writer.has("images/morphology") and not writer.has("images/morphology_3d")


# The kidney's alignment file: CRLF line ends, no header, no final newline.
KIDNEY_ALIGNMENT = (
    b"0.008566778149855193,1.288853807979006,914.1373717471324\r\n"
    b"-1.288853807979006,0.008566778149855193,32040.595506861275\r\n0,0,1"
)
KIDNEY_PIXEL = 0.21249222


def _apply(transform, point, steps=None):
    """Apply the stored steps to ``point``, a dict by axis name; each step reads its input axes by name."""
    for step in transform["transformations"][:steps]:
        names = [a["name"] for a in step["input"]["axes"]]
        vector = np.array([point[n] for n in names], dtype=float)
        if step["type"] == "affine":
            vector = np.array(step["affine"]) @ np.append(vector, 1.0)
        else:
            vector = vector * np.array(step["scale"])
        point = dict(zip((a["name"] for a in step["output"]["axes"]), vector, strict=True))
    return point


def test_the_stored_transform_places_the_kidney_he_pixels(tmp_path, writer):
    assert len(KIDNEY_ALIGNMENT) == 123
    he, alignment = tmp_path / "he.ome.tif", tmp_path / "alignment.csv"
    _rgb_pyramid(he, planar=False)
    alignment.write_bytes(KIDNEY_ALIGNMENT)
    write_he(writer, str(he), str(alignment), KIDNEY_PIXEL, concurrency=1)
    root = zarr.open_group(writer.repo.readonly_session(branch="work").store, mode="r")
    transform = root["images/he_image"].attrs["ome"]["multiscales"][0]["coordinateTransformations"][0]
    assert [a["name"] for a in transform["input"]["axes"]] == ["c", "y", "x"]
    for (x, y), morphology, micrometres in (
        ((0, 0), (914.137, 32040.596), (194.247, 6808.377)),
        ((23628, 0), (1116.553, 1587.558), (237.259, 337.344)),
    ):
        point = {"c": 0, "x": x, "y": y}
        got = _apply(transform, point, steps=1)
        assert (got["x"], got["y"]) == pytest.approx(morphology, abs=1e-3)
        got = _apply(transform, point)
        assert (got["x"], got["y"]) == pytest.approx(micrometres, abs=1e-3)
    assert read_alignment(KIDNEY_ALIGNMENT.decode())[0, 2] == 914.1373717471324


@pytest.fixture
def full_store(bundle, writer, tmp_path):
    """The three image elements written in full; ``he`` is the H&E object's path."""
    he, alignment = tmp_path / "he.ome.tif", tmp_path / "alignment.csv"
    _rgb_pyramid(he, planar=True)
    np.savetxt(alignment, ALIGNMENT, delimiter=",")
    write_morphology(writer, bundle, PIXEL, concurrency=2)
    write_morphology_3d(writer, bundle, PIXEL, concurrency=2)
    write_he(writer, str(he), str(alignment), PIXEL, concurrency=2)
    return writer.repo, str(he)


def _writable_root(repo):
    session = repo.writable_session("work")
    return session, zarr.open_group(session.store, mode="a")


def test_gate_images_passes_on_a_store_written_in_full(bundle, full_store):
    from verify import gate_images

    repo, he = full_store
    root = zarr.open_group(repo.readonly_session(branch="work").store, mode="r")
    assert gate_images(root, bundle, he) == []


@pytest.mark.parametrize(
    ("path", "index", "named"),
    [("images/morphology/s1", (2,), "morphology s1 channel 2"),
     ("images/morphology_3d/s2", (0, 2), "morphology_3d s2 plane 2"),
     ("images/morphology_3d/s0", (0, 1), "morphology_3d s0 plane 1"),
     ("images/he_image/s0", (slice(None),), "he_image s0: tile (0, 0)")],
)
def test_gate_images_names_a_corrupted_tile(bundle, full_store, path, index, named):
    from minus80.core.icechunk_io import commit
    from verify import gate_images

    repo, he = full_store
    session, root = _writable_root(repo)
    root[path][(*index, slice(0, 5), slice(0, 5))] += 1
    commit(session, "corrupt a tile")
    errors = gate_images(zarr.open_group(repo.readonly_session(branch="work").store, mode="r"), bundle, he)
    assert len(errors) == 1 and named in errors[0]


def test_gate_images_refuses_a_complete_element_that_lacks_a_level(bundle, full_store):
    from minus80.core.icechunk_io import commit
    from verify import gate_images

    repo, he = full_store
    session, root = _writable_root(repo)
    del root["images/morphology/s2"]
    commit(session, "drop a level")
    errors = gate_images(zarr.open_group(repo.readonly_session(branch="work").store, mode="r"), bundle, he)
    assert errors == ["images/morphology: marked complete, holds levels [0, 1] of 3"]
