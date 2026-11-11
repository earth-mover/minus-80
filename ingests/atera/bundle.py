"""A sample's ``outs.zip``: a stored zip of stored zips and OME-TIFFs, opened in place."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import tifffile
from minus80.core.registry import as_url
from minus80.core.remote import reader
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
DIFFEXP_AZIMUTH = "diffexp_genes/gene_expression_azimuth.csv"
DIFFEXP_GRAPHCLUST = "diffexp_genes/gene_expression_graphclust.csv"


def open_tiff(file) -> tifffile.TiffFile:
    # The OME-XML does not describe the pyramid's levels, so read it as a plain TIFF.
    return tifffile.TiffFile(file, is_ome=False)


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
        return open_tiff(self.member_file(name))

