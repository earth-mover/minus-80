"""The one-group-per-file layout reads back exactly what h5py decodes.

  1. byte-identical roundtrip: a numeric dataset read through the virtual store
     equals h5py's own decode of the same dataset (two independent IO paths);
  2. references only: the Icechunk repo is a tiny fraction of the source size;
  3. a multi-file collection rebuilds each file's numeric arrays exactly.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from minus80.core import as_url, icechunk_io  # noqa: E402
from minus80.parsers.nwb import open_collection, virtual_arrays  # noqa: E402


def _write(sources, repo_path) -> None:
    urls = [as_url(s) for s in sources]
    icechunk_io.write_manifest_group(open_collection(urls), repo_path, source_urls=urls)


def _reconstruct(repo_path) -> dict:
    """``{file_stem: {nwb_path: ndarray}}`` read back through the store."""
    per_file: dict[str, dict] = {}
    for full_path, arr in virtual_arrays(icechunk_io.open_repo(repo_path)).items():
        prefix, _, nwb_path = full_path.partition("/")
        per_file.setdefault(prefix, {})[nwb_path] = arr[...]
    return per_file


def _dir_size(path) -> int:
    return sum(
        os.path.getsize(os.path.join(root, f))
        for root, _, files in os.walk(path)
        for f in files
    )


def _assert_bitwise_equal(got: np.ndarray, exp: np.ndarray, label: str) -> None:
    # Identical bytes, not array_equal, which fails on the NaNs that position
    # tracking data legitimately contains.
    assert got.shape == exp.shape, f"{label}: shape {got.shape} != {exp.shape}"
    assert got.dtype == exp.dtype, f"{label}: dtype {got.dtype} != {exp.dtype}"
    assert got.tobytes() == exp.tobytes(), f"{label}: bytes differ"


def test_roundtrip_byte_identical(nwb_files, tmp_path):
    """Every virtualized numeric dataset equals h5py's own decode, byte for byte."""
    sample = nwb_files[0]
    _write([sample], tmp_path / "repo")
    ((stem, arrays),) = _reconstruct(tmp_path / "repo").items()

    assert arrays, "no numeric datasets were virtualized"
    with h5py.File(sample, "r") as f:
        for nwb_path, got in arrays.items():
            _assert_bitwise_equal(got, f[nwb_path][...], nwb_path)


def test_repo_is_references_only(nwb_files, tmp_path):
    """The Icechunk repo is a tiny fraction of the source (references, not bytes)."""
    sample = nwb_files[0]
    _write([sample], tmp_path / "repo")
    ratio = _dir_size(tmp_path / "repo") / os.path.getsize(sample)
    assert ratio < 0.05, f"repo/source size ratio {ratio:.4f} not << 1"


def test_collection_reconstructs(nwb_files, tmp_path):
    """A multi-file collection reconstructs each file's numeric arrays exactly."""
    _write(nwb_files, tmp_path / "repo")
    rebuilt = _reconstruct(tmp_path / "repo")

    assert len(rebuilt) == len(nwb_files), "expected one group per file"
    for src in nwb_files:
        stem = os.path.splitext(os.path.basename(src))[0]
        assert stem in rebuilt, f"{stem} missing from repo"
        with h5py.File(src, "r") as f:
            for nwb_path, got in rebuilt[stem].items():
                _assert_bitwise_equal(got, f[nwb_path][...], f"{stem}/{nwb_path}")
