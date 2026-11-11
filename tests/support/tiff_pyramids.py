"""Synthetic pyramid TIFFs shared by the pyramid tests and the Atera ingest tests."""

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


def pyramid_tiff(path, planes: int, shape=(700, 1300), levels=3, tile=(256, 256), description=None):
    """Write `planes` planes of lossless JPEG 2000 tiles, each with `levels` pyramid levels.

    `description` is the first page's ImageDescription, as OME-XML is in an OME-TIFF.
    """
    jp2 = dict(tile=tile, compression="jpeg2000", compressionargs={"reversible": True})
    with tifffile.TiffWriter(path, ome=False) as tw:
        for z in range(planes):
            data = picture(shape, seed=z)
            extra = {"description": description} if description and z == 0 else {}
            tw.write(data, subifds=levels - 1, **jp2, **extra)
            for _ in range(levels - 1):
                data = data[::2, ::2]
                tw.write(data, subfiletype=1, **jp2)
