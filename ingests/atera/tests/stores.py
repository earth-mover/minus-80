"""Synthetic Zarr v2 stores, zipped STORED, in the layout of an Atera bundle's inner zips."""

import zipfile

import numpy as np
import zarr


def zip_v2_store(tmp_path, name, arrays, attrs=None, group_attrs=None):
    """Write ``path -> (data, create_array kwargs)`` as a v2 store and zip it."""
    source = tmp_path / f"{name}.zarr"
    root = zarr.open_group(source, mode="w", zarr_format=2)
    root.attrs.update(attrs or {})
    for group, group_attr in (group_attrs or {}).items():
        root.require_group(group).attrs.update(group_attr)
    for path, (data, kwargs) in arrays.items():
        root.create_array(path, data=data, **kwargs)
    return zip_directory(source, tmp_path / f"{name}.zip")


def zip_directory(source, path):
    """Zip the files under ``source`` STORED at ``path``; the open zip."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(source).as_posix())
    return zipfile.ZipFile(path)


def cells_zip(tmp_path):
    """Two polygon sets over three cells: the nucleus set lacks cell 1 and has two parts for cell 2."""
    square, triangle = [0, 0, 1, 0, 1, 1, 0, 1, 0, 0], [2, 2, 4, 2, 3, 4, 0, 0, 0, 0]
    return zip_v2_store(tmp_path, "cells", {
        "cell_id": (np.array([[1, 1], [2, 1], [3, 1]], dtype="<u4"), {}),
        "polygon_sets/0/vertices": (np.array([square, square, triangle], dtype="<f4"), {}),
        "polygon_sets/0/num_vertices": (np.array([4, 4, 3], dtype="<i4"), {}),
        "polygon_sets/0/cell_index": (np.array([0, 2, 2], dtype="<u4"), {}),
        "polygon_sets/0/method": (np.array([3, 3, 4], dtype="<u4"), {}),
        "polygon_sets/1/vertices": (np.array([square, triangle, square], dtype="<f4"), {}),
        "polygon_sets/1/num_vertices": (np.array([4, 3, 4], dtype="<i4"), {}),
        "polygon_sets/1/cell_index": (np.array([0, 1, 2], dtype="<u4"), {}),
        "polygon_sets/1/method": (np.array([1, 1, 2], dtype="<u4"), {}),
    })
