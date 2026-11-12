"""A sample's ``outs.zip``: a stored zip of stored zips and OME-TIFFs, opened in place."""

from __future__ import annotations

import fnmatch
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import tifffile
from minus80.core.registry import as_url
from minus80.core.remote import reader
from minus80.parsers.zip.nested import member_file, member_zip

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
        infos = zipfile.ZipFile(self.file).infolist()
        self.names = [i.filename for i in infos]
        self.sizes = {i.filename: i.file_size for i in infos}

    def _members(self, pattern: str, count: int) -> list[str]:
        """The sorted members that match ``pattern``; ``ValueError`` unless there are exactly ``count``."""
        found = sorted(n for n in self.names if fnmatch.fnmatchcase(n, pattern))
        if len(found) != count:
            raise ValueError(f"expected {count} members that match {pattern}, found {len(found) or 'none'}: {found}")
        return found

    def morphology_2d(self) -> list[str]:
        """The four single-plane morphology pyramids, in name order."""
        return self._members("morphology_2d/ch*.ome.tif", 4)

    def morphology_3d(self) -> str:
        """The morphology stack."""
        return self._members("morphology_3d/ch*.ome.tif", 1)[0]

    def metrics(self) -> str:
        return self._members("metrics_summary*.csv", 1)[0]

    def report(self) -> str:
        return self._members("analysis_summary*.html", 1)[0]

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



def read_alignment(text: str) -> np.ndarray:
    """The 3 by 3 matrix of the alignment CSV, as published."""
    matrix = np.loadtxt(io.StringIO(text), delimiter=",", ndmin=2)
    if matrix.shape != (3, 3):
        raise ValueError(f"the alignment has shape {matrix.shape}, not 3 by 3")
    return matrix
