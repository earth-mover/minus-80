"""What TiffParser reads a file through, and what its group and arrays state."""

import pickle

import numpy as np
import pytest
import tifffile
from obspec_utils.registry import ObjectStoreRegistry
from obstore.store import MemoryStore
from virtualizarr.manifests import ManifestGroup, ManifestStore
from virtualizarr.parsers.typing import Parser

from minus80.core import as_url, registry_for
from minus80.ome import image_attrs
from minus80.parsers.tiff import TiffParser, parser
from minus80.parsers.tiff.inspect import DirectoryLayout


def _parse(source, **options) -> ManifestStore:
    url = as_url(source)
    return TiffParser(**options)(url, registry_for(url))


@pytest.fixture
def stack(tmp_path):
    """A BigTIFF of 100 small pages, whose directories sit at an even stride."""
    path = tmp_path / "stack.tif"
    tifffile.imwrite(path, np.arange(1600, dtype=np.uint8).reshape(100, 4, 4), bigtiff=True,
                     photometric="minisblack", metadata=None)
    return path


@pytest.fixture
def hyperstack(tmp_path):
    path = tmp_path / "hyperstack.tif"
    tifffile.imwrite(path, np.zeros((3, 2, 16, 16), np.uint16), imagej=True, resolution=(5, 5),
                     metadata={"spacing": 0.5, "unit": "um", "axes": "ZCYX"})
    return path


def test_follows_virtualizarr_parser_protocol():
    assert isinstance(TiffParser(), Parser)


def test_returns_a_group_in_a_store_over_the_registry(hyperstack):
    url = as_url(hyperstack)
    registry = registry_for(url)
    store = TiffParser()(url, registry)
    assert isinstance(store.group, ManifestGroup)
    assert store._registry is registry
    assert list(store.group) == ["0"]
    assert pickle.loads(pickle.dumps(store.group))["0"].shape == (3, 2, 16, 16)


def test_imagej_axes_are_typed(hyperstack):
    arr = _parse(hyperstack).group["0"]
    assert arr.metadata.dimension_names == ("z", "c", "y", "x")
    assert arr.metadata.attributes["dimensions"] == {
        "z": {"type": "space"}, "c": {"type": "channel"},
        "y": {"type": "space"}, "x": {"type": "space"}}


def test_typed_axes_reach_ome_zarr(hyperstack):
    arr = _parse(hyperstack).group["0"]
    attrs = arr.metadata.attributes
    dims = arr.metadata.dimension_names
    # OME-Zarr puts channel before space, so z, c, y, x cannot be written as is.
    with pytest.raises(ValueError, match="out of order"):
        image_attrs(name="h", dims=dims, level_shapes=[arr.shape],
                    scale={"c": 1, **attrs["scale"]}, dimensions=attrs["dimensions"])


def test_time_axis_is_time(tmp_path):
    path = tmp_path / "movie.tif"
    tifffile.imwrite(path, np.zeros((4, 8, 8), np.uint8), imagej=True, metadata={"axes": "TYX"})
    attrs = _parse(path).group["0"].metadata.attributes
    assert attrs["dimensions"]["t"] == {"type": "time"}


def test_an_unknown_axis_is_unnamed_and_untyped(stack):
    arr = _parse(stack).group["0"]
    assert arr.metadata.dimension_names == (None, "y", "x")
    assert arr.metadata.attributes["dimensions"] == {"y": {"type": "space"}, "x": {"type": "space"}}


def test_group_states_the_imagej_metadata_and_resolution_tags(hyperstack):
    attrs = _parse(hyperstack).group.metadata.attributes
    assert attrs["series"] == {"index": 0, "name": "", "kind": "imagej", "axes": "ZCYX",
                               "shape": [3, 2, 16, 16]}
    assert attrs["imagej"]["slices"] == 3 and attrs["imagej"]["channels"] == 2
    assert attrs["imagej"]["spacing"] == 0.5 and attrs["imagej"]["unit"] == "um"
    assert attrs["resolution"] == {"XResolution": [5, 1], "YResolution": [5, 1],
                                   "ResolutionUnit": "NONE"}
    assert "ome_pixels" not in attrs


def test_group_states_the_ome_pixel_sizes(tmp_path):
    path = tmp_path / "sized.ome.tif"
    tifffile.imwrite(path, np.zeros((3, 16, 16), np.uint8), ome=True,
                     metadata={"axes": "ZYX", "PhysicalSizeX": 200, "PhysicalSizeXUnit": "nm",
                               "PhysicalSizeY": 0.3, "PhysicalSizeZ": 2.0})
    pixels = _parse(path).group.metadata.attributes["ome_pixels"]
    assert pixels["PhysicalSizeX"] == 200 and pixels["PhysicalSizeXUnit"] == "nm"
    assert pixels["PhysicalSizeZ"] == 2.0


def test_manifest_records_the_url_given(stack):
    url = as_url(stack)
    arr = TiffParser()(url, registry_for(url)).group["0"]
    assert arr.metadata.attributes["source_url"] == url


def test_a_truncated_tiff_is_refused(tmp_path, range_server):
    path = tmp_path / "cut.tif"
    with tifffile.TiffWriter(path) as tif:
        for _ in range(3):
            tif.write(np.zeros((16, 16), np.uint8), contiguous=False, metadata=None)
    with tifffile.TiffFile(path) as tif:
        last = tif.pages[-1]
        end = last.dataoffsets[0] + last.databytecounts[0]
        assert end == path.stat().st_size
    # Cutting into the last strip leaves every directory readable.
    data = path.read_bytes()[:end - 10]
    path.write_bytes(data)
    with pytest.raises(ValueError, match=rf"ends at byte {end}, past the end of the {len(data)}-byte"):
        _parse(path)
    range_server.files["/cut.tif"] = data
    with pytest.raises(ValueError, match=rf"past the end of the {len(data)}-byte object"):
        _parse(f"{range_server.url}/cut.tif")


def test_an_unknown_prefetch_is_refused():
    with pytest.raises(ValueError, match="prefetch"):
        TiffParser(prefetch="all")


@pytest.mark.parametrize("prefetch", ["ranges", "directories", "whole"])
def test_remote_manifest_matches_local_parse(stack, range_server, monkeypatch, tmp_path, prefetch):
    monkeypatch.setenv("MINUS80_CACHE_DIR", str(tmp_path / "cache"))
    range_server.files["/stack.tif"] = stack.read_bytes()
    url = f"{range_server.url}/stack.tif"
    want = _parse(stack).group["0"]
    got = _parse(url, prefetch=prefetch).group["0"]
    assert got.shape == (100, 4, 4)
    assert got.metadata.attributes["source_url"] == url
    def ranges(arr):
        return {k: (e["offset"], e["length"]) for k, e in arr.manifest.dict().items()}

    assert ranges(got) == ranges(want)


def test_reads_go_through_the_registry_given(stack, range_server):
    range_server.files["/stack.tif"] = data = stack.read_bytes()
    url = f"{range_server.url}/stack.tif"
    memory = MemoryStore()
    memory.put("stack.tif", data)
    registry = ObjectStoreRegistry()
    registry.register(f"{range_server.url}/", memory)
    arr = TiffParser()(url, registry).group["0"]
    assert arr.metadata.attributes["source_url"] == url
    assert range_server.requests == []


def test_directories_prefetch_refuses_tags_outside_the_region(stack, range_server, monkeypatch, tmp_path):
    monkeypatch.setenv("MINUS80_CACHE_DIR", str(tmp_path / "cache"))
    range_server.files["/stack.tif"] = data = stack.read_bytes()
    with tifffile.TiffFile(stack) as tf:
        second = tf.pages[1].offset
    # A region that starts after the first directory leaves page 0's tags unfetched.
    monkeypatch.setattr(parser, "directory_layout", lambda source, registry: DirectoryLayout(
        len(data), second, True, directories=99))
    with pytest.raises(ValueError, match="outside the fetched"):
        _parse(f"{range_server.url}/stack.tif", prefetch="directories")


def test_directories_prefetch_refuses_a_classic_tiff(tmp_path, range_server):
    path = tmp_path / "classic.tif"
    tifffile.imwrite(path, np.zeros((4, 4), np.uint8))
    range_server.files["/classic.tif"] = path.read_bytes()
    with pytest.raises(ValueError, match="not bulk-readable"):
        _parse(f"{range_server.url}/classic.tif", prefetch="directories")
