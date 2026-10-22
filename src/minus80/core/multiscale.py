"""An OME-Zarr multiscale image with a virtual base level and stored downsampled levels.

:func:`write_multiscale` writes the full-resolution level as chunk references
into the source file and the downsampled levels as real chunks, in one Icechunk
OME-Zarr group. In a 3D pyramid halving each axis per level, the base is about
88% of the total, so storing only the upper levels costs about an eighth of a
full copy.

ngff-zarr computes the downsampled arrays and the ``multiscales`` metadata
(``to_ngff_image``, ``to_multiscales``, ``to_ngff_zarr``). What this module adds
is putting a virtual base level under that pyramid. docs/upstream.md tracks it
as an ngff-zarr and VirtualiZarr integration.

Two constraints shape the code:

1. Icechunk stores Zarr v3 only, and ngff-zarr writes OME-Zarr 0.4 as Zarr v2,
   so the metadata is written as OME-Zarr 0.5.
2. ``to_ngff_zarr`` calls ``zarr.consolidate_metadata``, which Icechunk does not
   support, and would write the base level as real chunks. So the pyramid is
   written once to an in-memory store with consolidation disabled, its ``ome``
   attributes are copied from there (``asdict`` on ngff-zarr's model does not
   reproduce its version-specific layout), and only the upper levels and the
   virtual base go into the repo.

The caller supplies the base level as a ``ManifestArray``, the full-resolution
data (read only to compute the upper levels), and the axis names and scale.
ngff-zarr is imported inside the functions that use it.
"""
from __future__ import annotations

import contextlib

import numpy as np
from virtualizarr.manifests import ManifestArray, ManifestGroup

from .icechunk_io import commit, create_repo


@contextlib.contextmanager
def _no_consolidate():
    import zarr

    orig = zarr.consolidate_metadata
    zarr.consolidate_metadata = lambda *a, **k: None  # type: ignore[assignment]
    try:
        yield
    finally:
        zarr.consolidate_metadata = orig


def _build_pyramid(full_res, *, name, dims, scale, axes_units, n_levels, method, downsample_dims):
    """The downsampled levels and their ``ome`` attributes, both from ngff-zarr."""
    import ngff_zarr as nz
    import zarr

    if downsample_dims is None:
        factors = [2**i for i in range(1, n_levels)]
    else:
        dd = set(downsample_dims)
        factors = [
            {d: (2**i if d in dd else 1) for d in dims} for i in range(1, n_levels)
        ]

    img = nz.to_ngff_image(
        np.asarray(full_res),
        dims=list(dims),
        scale=dict(scale),
        axes_units=(dict(axes_units) if axes_units else None),
        name=name,
    )
    multiscales = nz.to_multiscales(img, scale_factors=factors, method=method)

    mem = zarr.storage.MemoryStore()
    with _no_consolidate():
        nz.to_ngff_zarr(mem, multiscales, version="0.5")
    ome = dict(zarr.open_group(mem, mode="r").attrs)["ome"]
    return multiscales, ome


def write_multiscale(
    repo_path,
    *,
    name: str,
    base: ManifestArray,
    full_res,
    dims,
    scale,
    source_url: str,
    n_levels: int = 4,
    method=None,
    downsample_dims=None,
    axes_units=None,
    message: str = "virtualize hybrid multiscale",
) -> str:
    """Write an OME-Zarr multiscale: virtual level 0 (``base``) and stored levels 1..N.

    ``name`` is the OME-Zarr image name and ``base`` the virtual full-res level.
    ``full_res`` is the full-resolution array, used only to derive the
    small downsampled levels. ``dims``/``scale`` are ngff axis names and pixel sizes;
    ``downsample_dims`` restricts which axes are reduced (e.g. keep a stack/particle
    axis at 1x). ``method`` defaults to ``ITKWASM_BIN_SHRINK``. The repo is created
    at ``repo_path`` and authorized for ``source_url``. Returns the commit snapshot id.
    """
    import ngff_zarr as nz
    import zarr

    if method is None:
        method = nz.Methods.ITKWASM_BIN_SHRINK

    multiscales, ome = _build_pyramid(
        full_res,
        name=name,
        dims=dims,
        scale=scale,
        axes_units=axes_units,
        n_levels=n_levels,
        method=method,
        downsample_dims=downsample_dims,
    )
    paths = [d["path"] for d in ome["multiscales"][0]["datasets"]]

    repo = create_repo(repo_path, [source_url])
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="w")

    for i in range(1, len(paths)):
        data = np.asarray(multiscales.images[i].data)
        chunks = (1, *data.shape[1:]) if data.ndim > 1 else data.shape
        root.create_array(
            name=paths[i], shape=data.shape, dtype=data.dtype, chunks=chunks
        )[:] = data

    grp_path, arr_name = paths[0].rsplit("/", 1)
    ManifestGroup(arrays={arr_name: base}).to_icechunk(
        session.store, group=grp_path, mode="a"
    )
    root.attrs["ome"] = ome
    root.attrs["source_url"] = source_url
    return commit(session, message)
