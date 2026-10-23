"""Parse NWB (Neurodata Without Borders) files into VirtualiZarr references.

NWB is hierarchical HDF5 (``acquisition/``, ``processing/``, ``units/`` groups of
neurodata objects), so the parser is VirtualiZarr's ``HDFParser``. ``h5py`` is
used only to walk the tree and classify each leaf dataset: numeric (a
byte-range-referenceable chunk grid) or variable-length string / compound /
scalar (lives in the HDF5 global heap, not byte-range referenceable; see
``docs/formats.md``).

What this module adds is the NWB schema layer:

* walk the file, keep the numeric leaf datasets, namespace each by its full NWB
  path (``acquisition/ElectricalSeries/data``) so the whole hierarchy survives;
* run ``HDFParser`` once per *parent group*, dropping every non-numeric sibling,
  so the parser never touches a string/compound dataset (which makes it choke);
* the numeric arrays become byte-range references into the original file;
* the non-virtualizable data (strings, compound tables, scalars, object refs) and
  the NWB group attributes are read into memory and carried on the group's
  attributes, so a repo written from it is self-contained.

The result is a ``ManifestGroup``; write it with
:func:`minus80.core.icechunk_io.write_manifest_group`, which keeps each array's
own dimension names, so independent arrays (an ``ElectricalSeries`` ``data`` of
length N and its ``timestamps`` of a different length) never have to agree on a
shared axis. :mod:`minus80.parsers.nwb.datasets` builds the alternative layout
with shared, indexed axes.

    from minus80.parsers.nwb import open_collection
    group = open_collection(["a.nwb", "b.nwb"])   # many files -> one ManifestGroup
"""
from __future__ import annotations

import json
from pathlib import Path

from minus80.core import as_url, registry_for

# Numeric dtype kinds whose chunks are fixed-stride byte ranges we can reference:
# signed/unsigned int, float, complex, bool. Everything else (string 'S'/'U',
# object 'O', void/compound 'V') lives in the HDF5 heap and is written natively.
_NUMERIC_KINDS = frozenset("iufcb")


def _is_virtualizable(ds) -> bool:
    """True if an h5py dataset is a numeric N-D array we can byte-range reference.

    Excludes variable-length strings, fixed strings, compound/void dtypes, object
    references, scalars (0-D), and any zero-length axis — all of which either live
    in the HDF5 global heap or have no chunk grid to reference. Those go native.
    """
    import h5py

    dt = ds.dtype
    if dt.names is not None:  # compound / structured
        return False
    if h5py.check_string_dtype(dt) is not None:  # variable- or fixed-length string
        return False
    if dt.kind not in _NUMERIC_KINDS:  # 'O' object refs, 'S'/'U' bytes/str, 'V' void
        return False
    if ds.ndim < 1:  # scalars: tiny, no chunk grid — store natively
        return False
    if any(s == 0 for s in ds.shape):
        return False
    return True


def _sanitize(value):
    """Coerce an h5py-derived value into something JSON/zarr-attribute safe.

    HDF5 object references (NWB ``DynamicTableRegion.table``, ``.specloc``) and
    byte strings are not JSON serializable; recurse and stringify what isn't.
    """
    import math

    import numpy as np

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.ndarray):
        return _sanitize(value.tolist())
    if isinstance(value, np.generic):
        return _sanitize(value.item())
    if isinstance(value, float):
        # NWB stores unused amplifier settings (capacitance_slow, resistance_comp_*)
        # as NaN. Python's json emits a bare NaN/Infinity token, which is not valid
        # JSON and Icechunk's strict parser rejects; null is the compliant stand-in.
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    try:
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def _sanitize_attrs_inplace(attrs: dict) -> None:
    for k in list(attrs):
        attrs[k] = _sanitize(attrs[k])


def _native_value(ds):
    """Read a non-virtualizable dataset fully into a JSON-safe Python value.

    A dataset's own HDF5 attributes carry data the value alone cannot replace: NWB
    puts ``rate`` (and ``unit``) on the scalar ``starting_time``, so without them a
    regularly-sampled TimeSeries' time axis cannot be reconstructed from the repo.
    When a dataset has attributes the entry becomes ``{"value": …, "attrs": {…}}``;
    a bare value is kept as-is (native values are never dicts, so the two forms are
    unambiguous — see :func:`native_parts`).
    """
    value = _sanitize(ds[()])
    if not ds.attrs:
        return value
    return {"value": value, "attrs": _sanitize(dict(ds.attrs))}


def native_parts(entry) -> tuple[object, dict]:
    """Split a ``_nwb["native"]`` entry into ``(value, attrs)``, either form."""
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"], entry.get("attrs") or {}
    return entry, {}


def _walk(path: str):
    """Classify every leaf dataset in one NWB file.

    Returns ``(numeric_by_parent, native, group_attrs, root_attrs)``:

    * ``numeric_by_parent``: ``{parent_group_path: [wanted_child_names]}`` — the
      numeric leaves to virtualize, grouped so we can run ``HDFParser`` once per
      parent group with every non-numeric sibling dropped.
    * ``native``: ``{full_path: json_safe_value}`` — strings/compound/scalars.
    * ``group_attrs``: ``{group_path: attrs}`` — NWB neurodata_type metadata.
    * ``root_attrs``: attributes of the root ``NWBFile`` group.
    """
    import h5py

    numeric_by_parent: dict[str, list[str]] = {}
    native: dict[str, object] = {}
    group_attrs: dict[str, dict] = {}

    with h5py.File(path, "r") as f:
        root_attrs = _sanitize(dict(f["/"].attrs))

        def visit(name, obj):
            if isinstance(obj, h5py.Group):
                if obj.attrs:
                    group_attrs[name] = _sanitize(dict(obj.attrs))
                return
            # obj is a Dataset
            parent = obj.parent.name.lstrip("/")  # "" for root-level datasets
            if _is_virtualizable(obj):
                numeric_by_parent.setdefault(parent, []).append(name.rsplit("/", 1)[-1])
            else:
                native[name] = _native_value(obj)

        f.visititems(visit)
    return numeric_by_parent, native, group_attrs, root_attrs


def _manifest_arrays_for_file(url: str, registry, numeric_by_parent: dict):
    """Use HDFParser to build a ManifestArray for each numeric leaf dataset.

    One parser call per parent group: ``drop_variables`` names every direct child
    that is *not* a wanted numeric leaf (subgroups and string/compound datasets),
    so the parser builds only the arrays we asked for and never recurses into — or
    tries to construct a ManifestArray from — a heap-backed dataset.
    """
    import h5py
    from virtualizarr.parsers import HDFParser

    out = {}  # full NWB path -> ManifestArray
    with h5py.File(url_to_local(url), "r") as f:
        for parent, wanted in numeric_by_parent.items():
            group = f["/"] if parent == "" else f[parent]
            drop = [k for k in group.keys() if k not in wanted]
            parser = HDFParser(group=parent or "/", drop_variables=drop)
            manifest_group = parser(url, registry).group
            for leaf_name, marr in manifest_group.arrays.items():
                _sanitize_attrs_inplace(marr.metadata.attributes)
                full = leaf_name if parent == "" else f"{parent}/{leaf_name}"
                out[full] = marr
    return out


def url_to_local(url: str) -> str:
    """A local filesystem path for a source URL, so the parser can index it.

    ``file://`` (or bare) paths are used in place. Remote URLs (``http(s)``/cloud)
    are fetched once into ``data/_cache`` via parallel range GETs and indexed from
    that scratch copy -- the built repo still *references* the remote URL. HDF5's chunk B-tree is
    scattered through the file, so walking it over HTTP would be thousands of
    latency-bound round trips; pulling the file once is far faster."""
    from urllib.parse import unquote, urlparse

    p = urlparse(url)
    if p.scheme in ("", "file"):
        return unquote(p.path)
    if p.scheme in ("http", "https", "s3", "gs", "az"):
        from minus80.core.fetch import download_then_index

        return download_then_index(url, "data/_cache")
    raise ValueError(f"unsupported source url scheme for indexing: {url!r}")


def _unique(base: str, seen: set[str]) -> str:
    """A collision-free group name, suffixing ``_1``, ``_2`` … on clash."""
    name = base
    i = 1
    while name in seen:
        name = f"{base}_{i}"
        i += 1
    seen.add(name)
    return name


def _prefix_for(url: str, seen: set[str]) -> str:
    """A unique group name per file (its stem), so a collection's paths don't clash.

    For an ``s3://`` blob URL the stem is the content-addressed blob id (no filename),
    so pass an explicit ``names`` to :func:`open_collection` for meaningful group names
    (e.g. the archive's asset path ``sub-…/ses-…``).
    """
    return _unique(Path(url_to_local(url)).stem, seen)


def open_collection(sources, *, registry=None, names=None):
    """Virtualize a collection of NWB files as one readable ManifestGroup.

    Layout: one namespaced group per file (``<name>/<nwb/path>``); stack or select
    across files at read time. This is the collection-first product — single-file is
    just a one-element collection.

    ``names`` optionally gives an explicit group name per source (aligned with
    ``sources``), used instead of the file stem — pass the archive's asset path
    (``sub-…/ses-…``) so a collection built from ``s3://`` blob URLs gets meaningful,
    queryable session names rather than opaque blob ids.

    The returned :class:`~virtualizarr.manifests.ManifestGroup` holds the numeric
    arrays as virtual references. Its ``attributes`` carry, per file, the NWB root
    attributes, per-group attributes, and every non-virtualizable dataset loaded
    into memory (``_nwb`` key) — so a repo written from it is self-contained.
    """
    from virtualizarr.manifests import ManifestGroup

    urls = [as_url(s) for s in _as_list(sources)]
    if names is not None:
        names = list(names)
        if len(names) != len(urls):
            raise ValueError(f"names has {len(names)} entries for {len(urls)} sources")
    arrays: dict = {}
    per_file: dict = {}
    seen: set[str] = set()
    for i, url in enumerate(urls):
        prefix = _unique(names[i], seen) if names is not None else _prefix_for(url, seen)
        reg = registry or registry_for(url)
        local = url_to_local(url)
        numeric_by_parent, native, group_attrs, root_attrs = _walk(local)
        marrs = _manifest_arrays_for_file(url, reg, numeric_by_parent)
        for full_path, marr in marrs.items():
            # NWB files attach no HDF5 dimension scales, so HDFParser invents
            # phony_dim_<axis> for every axis. The file stated no name; leave it null.
            dims = marr.metadata.dimension_names
            if dims is not None:
                kept = tuple(None if n == f"phony_dim_{i}" else n for i, n in enumerate(dims))
                marr = marr.with_dimension_names(
                    None if all(n is None for n in kept) else kept
                )
            arrays[f"{prefix}/{full_path}"] = marr
        per_file[prefix] = {
            "source": url,
            "root_attrs": root_attrs,
            "group_attrs": group_attrs,
            "native": native,
        }
    attributes = {
        "format": "nwb",
        "n_files": len(urls),
        "files": list(per_file),
        "_nwb": per_file,  # native data + metadata; makes the repo self-contained
    }
    return ManifestGroup(arrays=arrays, attributes=attributes)


def virtual_arrays(grp) -> dict:
    """Map every virtual array in an opened repo to its full ``<file>/<nwb path>``.

    Each numeric dataset is stored at its own NWB path, so the logical dataset path
    is the array's path in the store. Returns ``{full_path: zarr.Array}``.
    """
    import zarr

    return {path: node for path, node in grp.members(max_depth=None)
            if isinstance(node, zarr.Array)}


def _as_list(x):
    return [x] if isinstance(x, (str, Path)) else list(x)
