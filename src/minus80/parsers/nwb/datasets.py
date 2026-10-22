"""One NWB file as an ``xr.DataTree``, with shared, indexed axes.

:func:`~minus80.parsers.nwb.parser.open_collection` keeps every array on its own
unnamed axes. This layout instead makes the store selectable without knowing NWB:

* one Dataset **per NWB group**, every child dataset a sibling variable (so
  ``ElectricalSeries`` holds ``data`` and ``timestamps`` together);
* shared dim names from the NWB schema: ``time`` for a TimeSeries, the group's
  own name for a DynamicTable's row axis;
* real index coordinates on those axes (sample times, row ids, electrode ids).
"""
from __future__ import annotations

from minus80.core import registry_for
from minus80.core.jsonable import jsonable

from .parser import _opened, file_contents

# A regular time axis is a linear ramp, which blosc's byte shuffle plus zstd compresses
# ~230x (a 2.88 M-sample 4.8 kHz axis: 23 MB of float64 down to 100 KB), so materializing
# it costs the repo almost nothing. The cap is about the transient array built during the
# write, not stored bytes; past it the axis is left to the reader to rebuild from the
# starting_time/rate attrs.
MAX_COORD_POINTS = 50_000_000

# A variable that labels another array's axis (channel ids, electrode ids) is small by
# nature; a large one is data that happens to match a length, so don't index on it.
MAX_INDEX_COORD = 1_000_000


def _primary_coord(ds, parent: str, primary_dim: str, leaves: dict, src):
    """Index the shared primary axis, so the opened Dataset is selectable.

    A TimeSeries gets ``time`` in seconds: an irregular one from its own ``timestamps``,
    a regular one computed from ``starting_time`` and the ``rate`` attribute that rides
    on it. A DynamicTable gets its NWB row ``id`` as the index of its row axis, so
    ``.sel(units=…)`` picks a unit by identity rather than by position.
    """
    import numpy as np

    if primary_dim not in ds.dims:
        return ds
    n = ds.sizes[primary_dim]
    if n > MAX_COORD_POINTS:
        return ds

    if primary_dim == "time":
        if "timestamps" in leaves:
            ds = _materialize(ds, "time", src[f"{parent}/timestamps"][()],
                              replaces="timestamps", units="seconds")
            # Recorded clocks are not always well behaved — one DANDI 000021 probe carries
            # 68 735 samples stamped -1 (the rig's "no valid time" sentinel) partway
            # through. Label-based slicing needs a sorted index, so say plainly whether
            # this axis has one; a reader checking an attribute beats a reader
            # discovering it from a pandas KeyError halfway through an analysis.
            ds["time"].attrs["monotonic"] = bool(
                np.all(np.diff(ds["time"].values) > 0)
            )
            return ds

        if "starting_time" not in leaves:
            return ds
        dset = src[f"{parent}/starting_time"]
        start, attrs = dset[()], jsonable(dict(dset.attrs))
        rate = attrs.get("rate")
        if dset.ndim or dset.dtype.kind not in "iuf" or not isinstance(rate, (int, float)) \
                or not rate:
            return ds
        values = float(start) + np.arange(n, dtype="float64") / rate
        ds = _materialize(ds, "time", values, units=attrs.get("unit", "seconds"))
        ds["time"].attrs["monotonic"] = True  # computed from a positive rate
        return ds

    if "id" in leaves and getattr(leaves["id"], "shape", (None,))[0] == n:
        return _materialize(ds, primary_dim, src[f"{parent}/id"][()], replaces="id")
    return ds


def _secondary_indexes(leaves: dict, primary) -> dict:
    """``{axis_length: leaf}`` for 1-D variables that *label another variable's axis*.

    An ``ElectricalSeries`` holds ``data`` of shape (time, channels) beside an
    ``electrodes`` column of length channels — the same channels, which the first-axis
    rule alone leaves on two unrelated dims (``data_1`` and ``electrodes_0``). A 1-D
    variable whose length matches some *non-first* axis is that axis' labels, so it
    names the dim and becomes its index. Ambiguity disqualifies: if two 1-D siblings
    share the length there is no way to tell which one labels the axis. A ragged
    column like ``units/spike_times`` matches no other axis and stays a variable.
    """
    other_axes = {
        s for m in leaves.values() for s in getattr(m, "shape", ())[1:]
    }
    out = {}
    for n in other_axes:
        if n == primary or n > MAX_INDEX_COORD:
            continue
        named = [leaf for leaf, m in leaves.items()
                 if getattr(m, "ndim", 0) == 1 and m.shape[0] == n]
        if len(named) == 1:
            out[n] = named[0]
    return out


def _index_values(src, path: str):
    """The labels a column contributes to the axis it indexes.

    Usually the column's own values. A ``DynamicTableRegion`` (NWB's foreign key — an
    ``ElectricalSeries``' ``electrodes`` column) instead stores *row positions* into
    the table its ``table`` attribute references, so its raw values are ``0, 1, 2 …``
    while the table it points at is indexed by real electrode ids. Left untranslated,
    two axes would both be called ``electrodes`` and carry different label spaces, and
    selecting across them would quietly return the wrong channels. Resolve the
    reference and hand back the ids, so both axes mean the same thing.
    """
    dset = src[path]
    values = dset[()]
    ref = dset.attrs.get("table")
    if ref is None:
        return values
    try:
        ids = src[ref]["id"][()]
        return ids[values]
    except Exception:  # unresolvable reference, or not a row-position column
        return values


def _placeholder(leaf: str) -> str:
    """Temporary dim name for an axis a variable is about to index.

    The axis wants the variable's own name, but xarray refuses to rename a dim to
    something a variable already answers to — that name only comes free once
    :func:`_materialize` has dropped the virtual variable, so carry a placeholder
    until then.
    """
    return f"{leaf}__idx"


def _materialize(ds, name: str, values, *, dim: str | None = None,
                 replaces: str | None = None, units: str | None = None):
    """Make ``name`` a real index coordinate over the dim of the same name.

    An index coordinate cannot stay virtual: xarray builds a `pandas` index over it, so
    the values must be in memory — which is why VirtualiZarr loads dimension coordinates
    rather than referencing them. These axes are the small ones (channel ids, row ids,
    sample times), and blosc byte-shuffle + zstd takes the cost to near nothing: a
    12.5 M-sample probe timestamp axis is 99.6 MB of float64 and 2.4 MB stored, where
    zarr's default zstd compresses it not at all.

    ``replaces`` names the virtual variable the coordinate supersedes (``timestamps``
    becoming ``time``, a table's ``id`` becoming its row axis) — it is dropped, since
    keeping both would list the same values twice under two names.
    """
    from zarr.codecs import BloscCodec

    drop = [v for v in (name, replaces) if v and v in ds.variables]
    ds = ds.drop_vars(drop)
    if dim and dim != name and dim in ds.dims:
        ds = ds.rename_dims({dim: name})
    ds = ds.assign_coords({name: (name, values)})
    if units:
        ds[name].attrs["units"] = units
    ds[name].encoding["compressors"] = (
        BloscCodec(cname="zstd", clevel=5, shuffle="shuffle"),
    )
    return ds


def _group_dataset(group: str, leaves: dict, attrs: dict, registry, src):
    """One NWB group as an ``xr.Dataset``, ready to select on.

    NWB's HDF5 datasets carry no dimension names, so every array would sit on its
    own unrelated axis. Per group:

    * the primary axis (``data``'s first axis, else the most common first-axis length)
      is ``time`` for a TimeSeries-like group, else the group's own name — so a
      DynamicTable's columns share one row axis instead of one dim each;
    * a 1-D variable that labels another variable's axis names that axis and indexes it
      (see :func:`_secondary_indexes`);
    * the axis that has an index gets a real coordinate (see :func:`_materialize`), so
      ``.sel`` works on an opened Dataset without the reader knowing NWB;
    * any remaining axis is ``<var>_<i>``.

    The file root has no primary axis: its datasets (``identifier``,
    ``file_create_date``) describe the session, not samples of one thing.
    """
    from collections import Counter

    import xarray as xr
    from virtualizarr.manifests import ManifestGroup, ManifestStore

    first_lens = {leaf: m.shape[0] for leaf, m in leaves.items() if m.ndim >= 1}
    primary = first_lens.get("data")
    if primary is None and first_lens and group:
        primary = Counter(first_lens.values()).most_common(1)[0][0]
    nd = str(attrs.get("neurodata_type", ""))
    time_like = "timestamps" in leaves or nd.endswith("Series")
    primary_dim = "time" if time_like else group.rsplit("/", 1)[-1]
    if primary_dim in leaves and primary_dim != "timestamps":
        primary_dim += "_rows"  # never collide with a same-named variable
    secondary = _secondary_indexes(leaves, primary)

    parts = []
    for leaf, marr in leaves.items():
        dims = []
        for i, n in enumerate(marr.shape):
            if i == 0 and n == primary:
                dims.append(primary_dim)
            elif n in secondary:
                dims.append(_placeholder(secondary[n]))
            else:
                dims.append(f"{leaf}_{i}")
        named = marr.with_dimension_names(dims or None)
        parts.append(ManifestStore(
            ManifestGroup(arrays={leaf: named}), registry=registry
        ).to_virtual_dataset(loadable_variables=[]))

    ds = xr.merge(parts) if parts else xr.Dataset()
    ds.attrs.update(attrs)
    for leaf in secondary.values():
        ds = _materialize(ds, leaf, _index_values(src, f"{group}/{leaf}"),
                          dim=_placeholder(leaf), replaces=leaf)
    return _primary_coord(ds, group, primary_dim, leaves, src)


def file_datatree(source, *, url=None, attributes=None):
    """One NWB file as an ``xr.DataTree`` whose nodes are the file's groups.

    ``source`` is a path, a URL or an open binary file; ``url`` is the address
    the manifests record, required for an open file. ``attributes`` are merged
    into the root node's. Write the tree with ``tree.vz.to_icechunk(store)``.
    """
    import h5py
    import xarray as xr

    with _opened(source, url) as (fh, recorded):
        contents = file_contents(fh, recorded)
        registry = registry_for(recorded)
        with h5py.File(fh, "r") as src:
            nodes = {f"/{group}": _group_dataset(group, arrays, attrs, registry, src)
                     for group, (arrays, attrs) in contents.items()}
    nodes["/"].attrs.update(jsonable(dict(attributes or {})))
    return xr.DataTree.from_dict(nodes)
