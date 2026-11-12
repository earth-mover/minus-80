"""Pages of synthetic TIFFs written into zarr levels, compared with tifffile's own decode."""

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import tifffile
import zarr

from zarr.storage import MemoryStore

from tiff_pyramids import picture as _picture, pyramid_tiff as _pyramid_tiff

from minus80.core import pyramid
from minus80.core.pyramid import (
    channel_names,
    check_page,
    create_level,
    physical_sizes,
    write_page,
    write_stack,
)

DECODE_POOL = "stack-decode"

XML = """<?xml version="1.0"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">
  <Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16">
    <Channel ID="Channel:0:0" Name="DAPI"/>
    <Channel ID="Channel:0:1" Name="ATP1A1/CD45/E-Cadherin"/>
    <Channel ID="Channel:0:2" Name="18S"/>
    <Channel ID="Channel:0:3" Name="alphaSMA/Vimentin"/>
  </Pixels></Image>
  <Image ID="Image:1"><Pixels ID="Pixels:1" DimensionOrder="XYZCT" Type="uint16">
    <Channel ID="Channel:1:0" Name="Other"/>
  </Pixels></Image>
</OME>"""


def _root(tmp_path):
    return zarr.open_group(tmp_path / "out.zarr", mode="w")


def _one_page(tmp_path, data, **kwargs):
    path = tmp_path / "one.tif"
    tifffile.imwrite(path, data, **kwargs)
    return tifffile.TiffFile(path)


def test_write_page_reproduces_a_lossless_jpeg2000_page(tmp_path):
    _pyramid_tiff(tmp_path / "p.tif", planes=1)
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        page = tif.series[0].levels[0].pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, *page.shape),
            page.dtype,
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        assert write_page(array, page, (0,)) == 3 * 6
        np.testing.assert_array_equal(array[0], page.asarray())


def test_edge_tiles_are_cropped(tmp_path):
    _pyramid_tiff(tmp_path / "p.tif", planes=1)
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        page = tif.series[0].levels[0].pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, *page.shape),
            page.dtype,
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        write_page(array, page, (0,))
        assert array.shape == (1, 700, 1300)
        np.testing.assert_array_equal(
            array[0, 512:, 1280:], page.asarray()[512:, 1280:]
        )


def test_planes_go_to_their_index_in_a_sharded_stack(tmp_path):
    _pyramid_tiff(tmp_path / "p.tif", planes=3, levels=1)
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        pages = tif.pages
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, 3, *pages[0].shape),
            pages[0].dtype,
            dims=("c", "z", "y", "x"),
            chunk=(256, 256),
            shard_planes=3,
        )
        for z in range(3):
            write_page(array, pages[z], (0, z))
        assert array.metadata.codecs[0].to_dict()["name"] == "sharding_indexed"
        assert array.chunks == (1, 1, 256, 256)
        assert array.shards == (1, 3, 256, 256)
        np.testing.assert_array_equal(array[0, 2], pages[2].asarray())


def test_create_level_chunks_and_dimension_names(tmp_path):
    array = create_level(
        _root(tmp_path), "s0", (3, 2048, 2048), "uint8", dims=("c", "y", "x")
    )
    assert array.chunks == (1, 1024, 1024)
    assert array.metadata.dimension_names == ("c", "y", "x")


def test_check_page_names_a_corrupted_tile(tmp_path):
    _pyramid_tiff(tmp_path / "p.tif", planes=1)
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        page = tif.series[0].levels[0].pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, *page.shape),
            page.dtype,
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        write_page(array, page, (0,))
        assert check_page(array, page, (0,), [(0, 0), (1, 1)]) == []
        array[0, 0:256, 0:256] = 0
        assert check_page(array, page, (0,), [(0, 0), (1, 1)]) == [
            "tile (0, 0) differs from the page"
        ]


def test_check_page_decodes_only_the_named_tiles(tmp_path, monkeypatch):
    with _one_page(
        tmp_path, _picture((700, 1300)), tile=(256, 256), compression="adobe_deflate"
    ) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, 700, 1300),
            "uint16",
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        write_page(array, page, (0,))
        decoded = []
        real = page.decode
        monkeypatch.setattr(
            type(page),
            "decode",
            property(
                lambda self: (
                    lambda data, i, **kw: (decoded.append(i), real(data, i, **kw))[1]
                )
            ),
        )
        assert check_page(array, page, (0,), [(0, 0), (2, 5)]) == []
        assert decoded == [0, 17]


def test_a_striped_page_is_refused(tmp_path):
    with _one_page(tmp_path, _picture((700, 1300))) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, 700, 1300),
            "uint16",
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        with pytest.raises(NotImplementedError, match="striped"):
            write_page(array, page, (0,))
        with pytest.raises(NotImplementedError, match="striped"):
            check_page(array, page, (0,), [(0, 0)])


def test_a_page_of_another_shape_is_refused(tmp_path):
    _pyramid_tiff(tmp_path / "p.tif", planes=1)
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        levels = tif.series[0].levels
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, *levels[0].pages[0].shape),
            "uint16",
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        with pytest.raises(ValueError, match="shape"):
            write_page(array, levels[1].pages[0], (0,))


def test_interleaved_rgb_deflate_tiles_land_channel_first(tmp_path):
    with _one_page(
        tmp_path,
        _picture((700, 1300, 3), np.uint8),
        tile=(256, 256),
        compression="adobe_deflate",
        photometric="rgb",
        planarconfig="contig",
    ) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (3, 700, 1300),
            "uint8",
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        write_page(array, page, ())
        np.testing.assert_array_equal(array[:], np.moveaxis(page.asarray(), -1, 0))
        assert check_page(array, page, (), [(0, 0), (2, 5)]) == []


def test_planar_rgb_uncompressed_tiles_land_channel_first(tmp_path):
    data = np.moveaxis(_picture((700, 1300, 3), np.uint8), -1, 0)
    with _one_page(
        tmp_path, data, tile=(256, 256), photometric="rgb", planarconfig="separate"
    ) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (3, 700, 1300),
            "uint8",
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        write_page(array, page, ())
        np.testing.assert_array_equal(array[:], page.asarray())
        assert check_page(array, page, (), [(0, 0), (2, 5)]) == []


def test_a_tile_smaller_than_the_chunk_still_writes_the_level(tmp_path):
    with _one_page(
        tmp_path, _picture((1500, 1100)), tile=(720, 544), compression="adobe_deflate"
    ) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, 1500, 1100),
            "uint16",
            dims=("c", "y", "x"),
            chunk=(1024, 1024),
        )
        write_page(array, page, (0,))
        np.testing.assert_array_equal(array[0], page.asarray())


def test_physical_sizes_come_from_the_first_images_pixels():
    xml = (
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image><Pixels PhysicalSizeX="0.5" PhysicalSizeY="0.25" PhysicalSizeZ="3.0"/></Image>'
        '<Image><Pixels PhysicalSizeX="9"/></Image></OME>'
    )
    assert physical_sizes(xml) == {"x": 0.5, "y": 0.25, "z": 3.0}
    assert physical_sizes(xml.replace(' PhysicalSizeZ="3.0"', "")) == {"x": 0.5, "y": 0.25}


def test_a_physical_size_in_another_unit_is_refused():
    xml = '<OME><Image><Pixels PhysicalSizeX="1" PhysicalSizeXUnit="mm"/></Image></OME>'
    with pytest.raises(ValueError, match="mm"):
        physical_sizes(xml)


def test_channel_names_from_ome_xml():
    assert channel_names(XML) == [
        "DAPI",
        "ATP1A1/CD45/E-Cadherin",
        "18S",
        "alphaSMA/Vimentin",
    ]


@pytest.mark.parametrize("tile", [(0, 6), (3, 0), (-1, 0)])
def test_check_page_refuses_a_tile_outside_the_grid(tmp_path, tile):
    # 700 x 1300 in 256-pixel tiles is 3 rows of 6 tiles.
    with _one_page(
        tmp_path, _picture((700, 1300)), tile=(256, 256), compression="adobe_deflate"
    ) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path), "s0", (1, 700, 1300), "uint16", dims=("c", "y", "x"), chunk=(256, 256)
        )
        with pytest.raises(IndexError, match=rf"tile \({tile[0]}, {tile[1]}\) is outside the 3 x 6 tile grid"):
            check_page(array, page, (0,), [tile])


def _count_pool_submits(monkeypatch):
    """Count tasks that write_page submits to its own pool, not tifffile's."""
    submitted = []

    class CountingPool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            submitted.append(args)
            return super().submit(*args, **kwargs)

    monkeypatch.setattr(pyramid, "ThreadPoolExecutor", CountingPool)
    return submitted


def test_concurrent_write_page_equals_serial(tmp_path):
    _pyramid_tiff(tmp_path / "p.tif", planes=1)
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        page = tif.series[0].levels[0].pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, *page.shape),
            page.dtype,
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        assert write_page(array, page, (0,), concurrency=8) == 18
        np.testing.assert_array_equal(array[0], page.asarray())


def test_unaligned_tiles_are_written_serially(tmp_path, monkeypatch):
    submitted = _count_pool_submits(monkeypatch)
    with _one_page(
        tmp_path, _picture((1500, 1100)), tile=(720, 544), compression="adobe_deflate"
    ) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (1, 1500, 1100),
            "uint16",
            dims=("c", "y", "x"),
            chunk=(1024, 1024),
        )
        write_page(array, page, (0,), concurrency=8)
        assert submitted == []
        np.testing.assert_array_equal(array[0], page.asarray())


def test_sharded_levels_are_written_serially(tmp_path, monkeypatch):
    submitted = _count_pool_submits(monkeypatch)
    with _one_page(
        tmp_path, _picture((512, 512)), tile=(256, 256), compression="adobe_deflate"
    ) as tif:
        page = tif.pages[0]
        array = create_level(
            _root(tmp_path),
            "s0",
            (2, 512, 512),
            "uint16",
            dims=("z", "y", "x"),
            chunk=(256, 256),
            shard_planes=2,
        )
        write_page(array, page, (1,), concurrency=8)
        assert submitted == []
        np.testing.assert_array_equal(array[1], page.asarray())


def test_a_failed_concurrent_write_page_raises_and_stops(tmp_path):
    class FailingStore(MemoryStore):
        sets = 0

        async def set(self, key, value, byte_range=None):
            if not key.endswith("zarr.json"):
                self.sets += 1
                if self.sets == 3:
                    raise OSError("write failed")
            await super().set(key, value, byte_range)

    _pyramid_tiff(tmp_path / "p.tif", planes=1)
    with tifffile.TiffFile(tmp_path / "p.tif") as tif:
        page = tif.series[0].levels[0].pages[0]
        store = FailingStore()
        array = create_level(
            zarr.group(store),
            "s0",
            (1, *page.shape),
            page.dtype,
            dims=("c", "y", "x"),
            chunk=(256, 256),
        )
        with pytest.raises(OSError, match="write failed"):
            write_page(array, page, (0,), concurrency=2)
        assert store.sets < 18


def test_write_page_refuses_concurrency_below_one(tmp_path):
    with _one_page(tmp_path, _picture((256, 256)), tile=(256, 256)) as tif:
        array = create_level(
            _root(tmp_path), "s0", (1, 256, 256), "uint16", dims=("c", "y", "x")
        )
        with pytest.raises(ValueError, match="concurrency"):
            write_page(array, tif.pages[0], (0,), concurrency=0)


def test_planes_sharing_a_chunk_are_written_serially(tmp_path, monkeypatch):
    submitted = _count_pool_submits(monkeypatch)
    with _one_page(
        tmp_path, _picture((512, 512)), tile=(256, 256), compression="adobe_deflate"
    ) as tif:
        page = tif.pages[0]
        array = _root(tmp_path).create_array(
            "z2", shape=(2, 512, 512), chunks=(2, 256, 256), dtype="uint16"
        )
        write_page(array, page, (1,), concurrency=8)
        assert submitted == []
        np.testing.assert_array_equal(array[1], page.asarray())


class _CountingStore(MemoryStore):
    """Counts `set` calls per chunk key."""

    def __init__(self):
        super().__init__()
        self.sets_per_key = {}

    async def set(self, key, value, byte_range=None):
        if not key.endswith("zarr.json"):
            self.sets_per_key[key] = self.sets_per_key.get(key, 0) + 1
        await super().set(key, value, byte_range)


def _stack(tmp_path, planes=3, store=None, **kwargs):
    """Return a sharded `(1, planes, y, x)` array and the pages of a TIFF with `planes` planes."""
    _pyramid_tiff(tmp_path / "p.tif", planes=planes, levels=1)
    tif = tifffile.TiffFile(tmp_path / "p.tif")
    pages = list(tif.pages)
    array = create_level(
        zarr.group(store) if store is not None else _root(tmp_path),
        "s0",
        (1, planes, *pages[0].shape),
        pages[0].dtype,
        dims=("c", "z", "y", "x"),
        chunk=(256, 256),
        shard_planes=planes,
        **kwargs,
    )
    return tif, pages, array


def test_write_stack_reproduces_every_plane(tmp_path):
    tif, pages, array = _stack(tmp_path)
    with tif:
        assert write_stack(array, pages, (0,)) == 18
        for z in range(3):
            np.testing.assert_array_equal(array[0, z], pages[z].asarray())


def test_write_stack_concurrent_equals_serial(tmp_path):
    tif, pages, array = _stack(tmp_path)
    with tif:
        assert write_stack(array, pages, (0,), concurrency=4) == 18
        for z in range(3):
            np.testing.assert_array_equal(array[0, z], pages[z].asarray())


def test_write_stack_writes_each_shard_once(tmp_path):
    store = _CountingStore()
    tif, pages, array = _stack(tmp_path, store=store)
    with tif:
        write_stack(array, pages, (0,))
        assert len(store.sets_per_key) == 18
        assert set(store.sets_per_key.values()) == {1}


def test_write_page_per_plane_writes_each_shard_once_per_plane(tmp_path):
    store = _CountingStore()
    tif, pages, array = _stack(tmp_path, store=store)
    with tif:
        for z in range(3):
            write_page(array, pages[z], (0, z))
        assert max(store.sets_per_key.values()) == 3


def test_write_stack_refuses_pages_of_different_shape(tmp_path):
    tif, pages, array = _stack(tmp_path)
    with tif, _one_page(tmp_path, _picture((256, 256)), tile=(256, 256)) as other:
        with pytest.raises(ValueError, match="shape"):
            write_stack(array, [pages[0], other.pages[0]], (0,))


def test_write_stack_refuses_a_plane_count_that_differs_from_the_array(tmp_path):
    tif, pages, array = _stack(tmp_path)
    with tif:
        with pytest.raises(ValueError, match="planes"):
            write_stack(array, pages[:2], (0,))


def test_write_stack_refuses_an_array_not_sharded_by_stack(tmp_path):
    tif, pages, array = _stack(tmp_path, planes=2)
    with tif:
        flat = zarr.group(MemoryStore()).create_array(
            "flat", shape=(1, 2, *pages[0].shape), chunks=(1, 1, 256, 256), dtype="uint16"
        )
        with pytest.raises(ValueError, match="shard"):
            write_stack(flat, pages, (0,))


@pytest.mark.parametrize("decoders", [1, 4])
def test_write_stack_bounds_pending_writes_by_bytes(tmp_path, monkeypatch, decoders):
    in_flight = []

    class TrackingPool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            if self._thread_name_prefix == DECODE_POOL:
                return future
            in_flight.append(future)
            in_flight[:] = [f for f in in_flight if not f.done()]
            peak.append(len(in_flight))
            return future

    peak = []
    monkeypatch.setattr(pyramid, "ThreadPoolExecutor", TrackingPool)
    # One region is 3 x 256 x 256 x 2 bytes; the bound allows two of them.
    monkeypatch.setattr(pyramid, "PENDING_BYTES", 2 * 3 * 256 * 256 * 2 + 1)
    tif, pages, array = _stack(tmp_path)
    with tif:
        assert write_stack(array, pages, (0,), concurrency=8, decoders=decoders) == 18
        assert max(peak) <= 2
        np.testing.assert_array_equal(array[0, 1], pages[1].asarray())


def test_write_stack_refuses_pages_of_different_tile_size(tmp_path):
    tif, pages, array = _stack(tmp_path, planes=1)
    with tif, _one_page(
        tmp_path, _picture((700, 1300)), tile=(128, 128)
    ) as other:
        wide = zarr.group(MemoryStore()).create_array(
            "w", shape=(1, 2, 700, 1300), chunks=(1, 1, 256, 256), dtype="uint16"
        )
        with pytest.raises(ValueError, match="tile size"):
            write_stack(wide, [pages[0], other.pages[0]], (0,))


def test_write_stack_refuses_empty_pages(tmp_path):
    tif, pages, array = _stack(tmp_path)
    with tif:
        with pytest.raises(ValueError, match="no pages"):
            write_stack(array, [], (0,))


def test_write_stack_refuses_a_wrong_index_length(tmp_path):
    tif, pages, array = _stack(tmp_path)
    with tif:
        with pytest.raises(ValueError, match="index"):
            write_stack(array, pages, (0, 0))


def test_write_stack_refuses_a_shard_longer_than_one_before_the_last_three(tmp_path):
    tif, pages, _ = _stack(tmp_path, planes=2)
    with tif:
        array = zarr.group(MemoryStore()).create_array(
            "s", shape=(2, 2, *pages[0].shape), chunks=(1, 1, 256, 256),
            shards=(2, 2, 256, 256), dtype="uint16",
        )
        with pytest.raises(ValueError, match="shard"):
            write_stack(array, pages, (0,))


def test_write_stack_fills_a_missing_tile_with_the_fill_value(tmp_path):
    tif, pages, _ = _stack(tmp_path, planes=2)
    with tif:
        array = zarr.group(MemoryStore()).create_array(
            "f", shape=(1, 2, *pages[0].shape), chunks=(1, 1, 256, 256),
            shards=(1, 2, 256, 256), dtype="uint16", fill_value=7,
        )
        pages[1].databytecounts = (0, *pages[1].databytecounts[1:])
        write_stack(array, pages, (0,))
        assert (array[0, 1, :256, :256] == 7).all()
        np.testing.assert_array_equal(array[0, 0], pages[0].asarray())


def _pool_threads():
    return [t for t in threading.enumerate() if t.name.startswith(DECODE_POOL)]


@pytest.mark.parametrize("concurrency", [1, 4])
def test_write_stack_decoded_in_threads_equals_serial(tmp_path, concurrency):
    tif, pages, serial = _stack(tmp_path, planes=5)
    with tif:
        threaded = create_level(
            zarr.group(MemoryStore()), "s0", serial.shape, serial.dtype,
            dims=("c", "z", "y", "x"), chunk=(256, 256), shard_planes=5,
        )
        pages[3].databytecounts = (0, *pages[3].databytecounts[1:])
        assert write_stack(serial, pages, (0,), concurrency=concurrency) == 18
        assert write_stack(threaded, pages, (0,), concurrency=concurrency, decoders=4) == 18
        np.testing.assert_array_equal(threaded[:], serial[:])
        np.testing.assert_array_equal(threaded[0, 2], pages[2].asarray())
        assert (threaded[0, 3, :256, :256] == 0).all()
        assert not _pool_threads()


def test_write_stack_uses_no_pool_with_one_decoder(tmp_path, monkeypatch):
    submitted = _count_pool_submits(monkeypatch)
    tif, pages, array = _stack(tmp_path)
    with tif:
        write_stack(array, pages, (0,), decoders=1)
    assert submitted == []


def test_a_decode_that_raises_stops_the_write_and_ends_the_pool(tmp_path, monkeypatch):
    tif, pages, array = _stack(tmp_path, planes=5)
    real_decode = pyramid._decode_segments
    plane_two = []

    def failing(page, segments):
        if page is pages[2]:
            plane_two.append(1)
            if len(plane_two) == 2:
                raise OSError("decode failed")
        return real_decode(page, segments)

    writes = []
    real_set = zarr.Array.__setitem__
    monkeypatch.setattr(pyramid, "_decode_segments", failing)
    monkeypatch.setattr(zarr.Array, "__setitem__", lambda a, k, v: writes.append(k) or real_set(a, k, v))
    with tif:
        with pytest.raises(OSError, match="decode failed"):
            write_stack(array, pages, (0,), decoders=4)
    assert len(writes) == 1
    assert not _pool_threads()


def test_write_stack_refuses_decoders_below_one(tmp_path):
    tif, pages, array = _stack(tmp_path)
    with tif:
        with pytest.raises(ValueError, match="decoders must be at least 1"):
            write_stack(array, pages, (0,), decoders=0)
