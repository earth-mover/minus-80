"""Synthetic test images: a noisy picture, and a tiled pyramid TIFF of one plane or a ZYX stack."""

import numpy as np
import tifffile


def picture(shape, dtype=np.uint16, seed=0):
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


def pyramid_tiff(
    path, planes: int, shape=(700, 1300), levels=3, tile=(256, 256), description=None, metadata=None
):
    """Write lossless JPEG 2000 tiles with `levels` pyramid levels.

    One plane is a 2D page whose first `description` is the ImageDescription, as OME-XML is in an
    OME-TIFF. With `metadata` (OME keys, such as `PhysicalSizeZ`) the planes are one ZYX OME series,
    each page with one SubIFD per level.
    """
    jp2 = dict(tile=tile, compression="jpeg2000", compressionargs={"reversible": True})
    data = np.stack([picture(shape, seed=z) for z in range(planes)])
    if metadata is None and planes == 1:
        with tifffile.TiffWriter(path, ome=False) as tw:
            extra = {"description": description} if description else {}
            level = data[0]
            tw.write(level, subifds=levels - 1, **jp2, **extra)
            for _ in range(levels - 1):
                level = level[::2, ::2]
                tw.write(level, subfiletype=1, **jp2)
        return
    with tifffile.TiffWriter(path, ome=True) as tw:
        tw.write(data, subifds=levels - 1, metadata={"axes": "ZYX", **(metadata or {})}, **jp2)
        for _ in range(levels - 1):
            data = data[:, ::2, ::2]
            tw.write(data, subfiletype=1, **jp2)
