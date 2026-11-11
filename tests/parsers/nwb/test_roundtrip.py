"""The one-group-per-file layout reads back what h5py decodes, from references and inlined values."""

from __future__ import annotations

import os
import posixpath

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from minus80.core import as_url, icechunk_io  # noqa: E402
from minus80.parsers.nwb import open_collection, virtual_arrays  # noqa: E402
from minus80.parsers.nwb.parser import NATIVE  # noqa: E402


def _expected(f, path: str):
    """What the store should hold for one dataset, decoded by h5py."""
    ds = f[path]
    if h5py.check_vlen_dtype(ds.dtype) in (str, bytes):
        return np.asarray(ds.asstr()[()], dtype=object)
    if h5py.check_ref_dtype(ds.dtype) is h5py.Reference:
        return np.vectorize(lambda r: f[r].name if r else "", otypes=[object])(ds[()])
    return np.asarray(ds[()])


def _absolute(target: str, holder: str) -> str:
    """An object reference stored relative to the array that holds it, made absolute."""
    return posixpath.normpath(posixpath.join(holder, target)) if target else ""


def test_every_dataset_reads_back_as_h5py_decodes_it(nwb_files, tmp_path):
    """Numbers byte for byte, strings as text, references as their targets' paths."""
    urls = [as_url(s) for s in nwb_files]
    repo = tmp_path / "repo"
    icechunk_io.write_manifest_group(open_collection(nwb_files), repo, source_urls=urls)
    stored = virtual_arrays(icechunk_io.open_repo(repo))

    for src in nwb_files:
        stem = os.path.splitext(os.path.basename(src))[0]
        with h5py.File(src, "r") as f:
            paths = []
            f.visititems(lambda n, o: paths.append(n) if isinstance(o, h5py.Dataset) else None)
            assert {f"{stem}/{p}" for p in paths} <= set(stored), "a dataset is missing"
            for path in paths:
                got, exp = np.asarray(stored[f"{stem}/{path}"][...]), _expected(f, path)
                if exp.dtype.names:
                    for field in exp.dtype.names:
                        if exp.dtype[field].kind == "O":
                            refs = [_absolute(t, f"/{path}") for t in got[field].ravel()]
                            assert refs == [f[r].name for r in exp[field].ravel()], path
                        else:
                            assert np.array_equal(got[field], exp[field]), path
                elif exp.dtype.kind == "O" and h5py.check_ref_dtype(f[path].dtype):
                    absolute = np.vectorize(lambda t: _absolute(t, f"/{path}"), otypes=[object])
                    assert np.array_equal(absolute(got), exp), path
                elif exp.dtype.kind == "O":
                    assert np.array_equal(np.asarray(got, dtype=object), exp), path
                else:
                    # Bytes, not array_equal: position tracking data holds NaNs.
                    assert got.dtype == exp.dtype and got.shape == exp.shape, path
                    assert np.asarray(got).tobytes() == exp.tobytes(), path

    repo_bytes = sum(os.path.getsize(os.path.join(d, n))
                     for d, _, names in os.walk(repo) for n in names)
    assert repo_bytes < 0.1 * sum(os.path.getsize(s) for s in nwb_files)


def test_open_file_records_the_url_given(nwb_files):
    url = "https://example.org/dandiset/session.nwb"
    with open(nwb_files[0], "rb") as fh:
        group = open_collection([fh], urls=[url])
        with pytest.raises(ValueError, match="url="):
            open_collection([fh])

    (name,) = group.groups
    data = group.groups[name]["acquisition"]["ElectricalSeries"]["data"]
    assert name == "session"
    assert data.metadata.attributes["source_url"] == url
    assert set(data.manifest.iter_nonempty_paths()) == {url}


def test_datasets_hdfparser_refuses_are_read_natively(tmp_path):
    """A region reference makes HDFParser raise; a compact dataset it reads as fill.

    Both are read with h5py onto their group's attributes, and their siblings
    stay arrays.
    """
    path = tmp_path / "edge.h5"
    with h5py.File(path, "w") as f:
        g = f.create_group("g")
        g["numbers"] = np.arange(6, dtype="f8")
        regions = g.create_dataset("regions", (1,), dtype=h5py.regionref_dtype)
        regions[0] = g["numbers"].regionref[1:3]
        plist = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        plist.set_layout(h5py.h5d.COMPACT)
        compact = h5py.h5d.create(g.id, b"compact", h5py.h5t.NATIVE_INT32,
                                  h5py.h5s.create_simple((3,)), dcpl=plist)
        compact.write(h5py.h5s.ALL, h5py.h5s.ALL, np.array([4, 5, 6], dtype="i4"))

    group = open_collection(str(path)).groups["edge"].groups["g"]

    assert set(group.arrays) == {"numbers"}
    native = group.metadata.attributes[NATIVE]
    assert native["compact"]["value"] == [4, 5, 6]
    assert set(native) == {"compact", "regions"}


@pytest.mark.xfail(strict=True, reason=(
    "Indexing works; Icechunk then finds no virtual chunk container for the "
    "percent-encoded file:// chunk location, though the container's prefix is "
    "the same encoded string. Not yet diagnosed."))
def test_directory_with_space_and_plus_round_trips(nwb_files, tmp_path):
    odd = tmp_path / "sp ace+plus"
    odd.mkdir()
    copy = odd / "session.nwb"
    copy.write_bytes(open(nwb_files[0], "rb").read())

    repo = tmp_path / "repo"
    icechunk_io.write_manifest_group(open_collection(str(copy)), repo,
                                     source_urls=[as_url(copy)])
    got = icechunk_io.open_repo(repo)["session/acquisition/ElectricalSeries/data"][...]
    with h5py.File(copy, "r") as f:
        assert np.array_equal(got, f["acquisition/ElectricalSeries/data"][()])
