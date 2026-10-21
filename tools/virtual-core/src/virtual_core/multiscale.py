"""Hybrid virtual + native OME-Zarr multiscale (staged for upstream — see ../../UPSTREAM.md).

Virtualize the full-resolution **base** level (references, no copy) and materialize
the small downsampled levels **natively**, in one Icechunk OME-Zarr group. In a 3D
2x pyramid the base is ~88% of the total, so this saves the overwhelming majority of
storage versus a full OME-Zarr copy while still giving a reader responsive multiscale.

**Buy the pyramid.** ngff-zarr computes the downsampled arrays AND the OME-Zarr
``multiscales`` metadata (``to_ngff_image`` → ``to_multiscales`` → ``to_ngff_zarr``).
The one thing built here is injecting a VIRTUAL base scale into an otherwise-native
ngff-zarr pyramid — an ngff-zarr × virtualizarr integration, and the upstream target.

Two icechunk quirks worked around (both icechunk limitations, not our metadata):
1. ngff-zarr's OME 0.4 = Zarr v2 metadata, which icechunk rejects → use OME **0.5**.
2. ``to_ngff_zarr`` always calls ``zarr.consolidate_metadata`` (unsupported by
   icechunk) and would materialize the native base. So we capture ngff-zarr's exact
   ``ome`` dict from a transient in-memory write (the only faithful serializer —
   ``asdict`` doesn't reproduce ngff's version-specific flattening), then write only
   the native upper levels + inject the virtual base into the repo ourselves.

Requires ngff-zarr (the ``imaging`` extra); the base package does not import it.
Format subprojects supply the base level as a ``ManifestArray``, the
full-resolution array (used only to derive the tiny upper levels), and the axis
names/scale — nothing format-specific lives here.
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
    """BUY: ngff-zarr computes the pyramid arrays + captures its exact OME metadata."""
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
    """Write an OME-Zarr multiscale: virtual base (``base``) + native levels 1..N.

    ``name`` is the OME-Zarr image name and ``base`` the virtual full-res level.
    ``full_res`` is the full-resolution array, used only to derive the
    small downsampled levels. ``dims``/``scale`` are ngff axis names and pixel sizes;
    ``downsample_dims`` restricts which axes are reduced (e.g. keep a stack/particle
    axis at 1x). Returns the commit snapshot id.
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

    # Native downsampled levels (ngff-zarr's arrays, real chunks).
    for i in range(1, len(paths)):
        data = np.asarray(multiscales.images[i].data)
        chunks = (1, *data.shape[1:]) if data.ndim > 1 else data.shape
        root.create_array(
            name=paths[i], shape=data.shape, dtype=data.dtype, chunks=chunks
        )[:] = data

    # Virtual base level, beside the native levels already in the group.
    grp_path, arr_name = paths[0].rsplit("/", 1)
    ManifestGroup(arrays={arr_name: base}).to_icechunk(
        session.store, group=grp_path, mode="a"
    )
    root.attrs["ome"] = ome
    root.attrs["source_url"] = source_url
    return commit(session, message)
