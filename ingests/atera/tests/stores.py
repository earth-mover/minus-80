"""Synthetic Zarr v2 stores, zipped STORED, in the layout of an Atera bundle's inner zips."""

import zipfile

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
    path = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(source).as_posix())
    return zipfile.ZipFile(path)
