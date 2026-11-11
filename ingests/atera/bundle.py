"""A sample's ``outs.zip``: a stored zip of stored zips and OME-TIFFs, opened in place."""

from __future__ import annotations

import itertools
import json
import math
import zipfile
from pathlib import Path

import numcodecs
import numpy as np
import tifffile
from minus80.core.registry import as_url
from minus80.core.remote import reader
from minus80.core.zarr_copy import v2_spec
from minus80.parsers.zip.nested import member_file, member_zip

MORPHOLOGY_2D = (
    "morphology_2d/ch0000_dapi.ome.tif",
    "morphology_2d/ch0001_atp1a1_cd45_e-cadherin.ome.tif",
    "morphology_2d/ch0002_18s.ome.tif",
    "morphology_2d/ch0003_alphasma_vimentin.ome.tif",
)
MORPHOLOGY_3D = "morphology_3d/ch0000_dapi_3d.ome.tif"
CELLS = "cells.zarr.zip"
TRANSCRIPTS = "transcripts.zarr.zip"
MATRIX = "cell_feature_matrix.zarr.zip"


class Bundle:
    """The members of one ``outs.zip``, at a local path or a URL."""

    def __init__(self, location: str | Path, file=None):
        self.file = file if file is not None else reader(as_url(location))
        self.names = zipfile.ZipFile(self.file).namelist()

    def member_file(self, name: str):
        return member_file(self.file, name)

    def member_zip(self, name: str):
        return member_zip(self.file, name)

    def experiment(self) -> dict:
        """The parsed ``experiment.spatial``."""
        with self.member_file("experiment.spatial") as f:
            return json.load(f)

    def tiff(self, name: str) -> tifffile.TiffFile:
        # The OME-XML does not describe the pyramid's levels, so read it as a plain TIFF.
        return tifffile.TiffFile(self.member_file(name), is_ome=False)



def read_v2_array(zf: zipfile.ZipFile, path: str) -> np.ndarray:
    """Decode a whole Zarr v2 array of numeric dtype from ``zf``; a missing chunk is the fill value."""
    zarray = json.loads(zf.read(f"{path}/.zarray"))
    spec = v2_spec(zarray)
    separator = zarray.get("dimension_separator", ".")
    dtype = np.dtype(spec.dtype)
    codecs = [spec.compressor, *reversed(spec.filters)]
    out = np.full(spec.shape, spec.fill_value or 0, dtype=dtype, order=spec.order)
    grid = [math.ceil(n / c) for n, c in zip(spec.shape, spec.chunks)]
    members = set(zf.namelist())
    for index in itertools.product(*map(range, grid)):
        member = f"{path}/{separator.join(map(str, index))}"
        if member not in members:
            continue
        raw = zf.read(member)
        for codec in codecs:
            if codec is not None:
                raw = numcodecs.get_codec(dict(codec)).decode(raw)
        chunk = np.frombuffer(raw, dtype=dtype).reshape(spec.chunks, order=spec.order)
        # Edge chunks are stored padded to the full chunk shape.
        start = [i * c for i, c in zip(index, spec.chunks)]
        window = tuple(slice(s, min(s + c, n)) for s, c, n in zip(start, spec.chunks, spec.shape))
        out[window] = chunk[tuple(slice(0, w.stop - w.start) for w in window)]
    return out
