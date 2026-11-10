"""Pages of synthetic TIFFs written into zarr levels, compared with tifffile's own decode."""

import numpy as np
import pytest
import tifffile
import zarr

from minus80.core.pyramid import channel_names, check_page, create_level, write_page

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


def _picture(shape, dtype=np.uint16, seed=0):
    """Smooth content with some noise, so every tile differs."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[: shape[0], : shape[1]]
    base = (np.sin(y / 17) + np.cos(x / 23)) * 60 + 128 + rng.normal(0, 4, shape[:2])
    out = (
        np.stack([base + 20 * k for k in range(shape[2])], -1)
        if len(shape) == 3
        else base
    )
    return np.clip(out, 0, 255).astype(dtype)


def _pyramid_tiff(path, planes: int, shape=(700, 1300), levels=3):
    """Write `planes` planes of lossless JPEG 2000 tiles, each with `levels` pyramid levels."""
    jp2 = dict(
        tile=(256, 256), compression="jpeg2000", compressionargs={"reversible": True}
    )
    with tifffile.TiffWriter(path, ome=False) as tw:
        for z in range(planes):
            data = _picture(shape, seed=z)
            tw.write(data, subifds=levels - 1, **jp2)
            for _ in range(levels - 1):
                data = data[::2, ::2]
                tw.write(data, subfiletype=1, **jp2)


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


def test_channel_names_from_ome_xml():
    assert channel_names(XML) == [
        "DAPI",
        "ATP1A1/CD45/E-Cadherin",
        "18S",
        "alphaSMA/Vimentin",
    ]
