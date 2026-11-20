"""Write an HDF5 NWB file into Icechunk in the unified NWB Zarr v3 convention.

hdmf-zarr 0.14 reads and writes NWB in Zarr v3 under a convention it shares
with LINDI and shadowzarr: links in a ``_LINKS`` group attribute, dataset types
in ``_DTYPE``, scalars as shape-() arrays, references as ``{"_REFERENCE":
{"source", "path"}}`` (hdmf-zarr's ``docs/source/storage.rst``). A store
holding one file at its root opens with ``hdmf_zarr.NWBZarrIO`` as a typed
``NWBFile``.

shadowzarr does the mapping: ``generate_rfs`` reads the file's headers and
returns the whole tree as Zarr v3 metadata, chunk references into the file and
small datasets inline, and ``rfs_to_icechunk`` writes that into an Icechunk
session, the references as virtual chunks carrying the file's ETag. This
module adds what an archive of many files needs on top:

- :func:`under_group` places the file at a group of a larger store, because
  ``rfs_to_icechunk`` writes only at a session's root;
- :func:`name_dimensions` gives every array ``dimension_names``, which the
  convention leaves out and xarray needs, so a TimeSeries or table group also
  opens with ``xr.open_zarr``.

A file whose HDF5 filters are gzip or none needs no minus80 and no shadowzarr
to read: its codecs are Zarr's own and numcodecs' ``zlib``, which zarr-python
installs. hdmf-zarr opens only a
store's root (``NWBZarrIO(path=...)`` takes a path or a store, not a group), so
pynwb reads a file placed at a group only once hdmf-zarr accepts one; zarr and
xarray read it now. docs/upstream.md tracks the gaps.
"""

from __future__ import annotations

import json
from collections.abc import Callable


def nwb_references(url: str) -> dict:
    """The file at ``url`` (http or https) as a shadowzarr reference file system."""
    from shadowzarr import generate_rfs

    return generate_rfs(url)


def under_group(rfs: dict, group: str) -> dict:
    """The same reference file system with every key moved under ``group`` ("" for the root)."""
    if not group.strip("/"):
        return rfs
    if rfs.get("gen"):
        raise NotImplementedError("moving gen entries under a group")
    prefix = group.strip("/") + "/"
    moved = dict(rfs)
    moved["refs"] = {prefix + key: value for key, value in rfs["refs"].items()}
    moved["indexes"] = {prefix + key: value for key, value in rfs.get("indexes", {}).items()}
    return moved


def name_dimensions(rfs: dict, *, channel_series: Callable[[dict], bool] | None = None) -> dict:
    """Give every array ``dimension_names``, choosing names that line up within a group.

    NWB's schema names axes per dataset (an ElectricalSeries' ``data`` is
    ``num_times, num_channels`` and its ``electrodes`` ``num_rows``), so its
    names do not line up between siblings. These do:

    - in a TimeSeries, ``data`` and every array as long as its ``timestamps``
      start with ``time``; an ElectricalSeries' ``data`` then has ``channel``,
      the axis its ``electrodes`` region runs along;
    - in a table (a group with ``id``), every column as long as ``id`` starts
      with the table's own name;
    - any other axis is ``<array>_dim<n>``, so every group still opens in xarray.

    Names already set are kept.
    """
    refs = rfs["refs"]
    arrays = {key[: -len("/zarr.json")]: json.loads(value) for key, value in refs.items()
              if key.endswith("/zarr.json") and isinstance(value, str)
              and json.loads(value)["node_type"] == "array"}
    groups = {key[: -len("/zarr.json")] if "/" in key else "": json.loads(value)
              for key, value in refs.items()
              if (key == "zarr.json" or key.endswith("/zarr.json")) and isinstance(value, str)
              and json.loads(value)["node_type"] == "group"}

    def length(path: str) -> int | None:
        meta = arrays.get(path)
        return meta["shape"][0] if meta and meta["shape"] else None

    for path, meta in arrays.items():
        if meta.get("dimension_names"):
            continue
        parent, _, name = path.rpartition("/")
        group_type = groups.get(parent, {}).get("attributes", {}).get("neurodata_type")
        shape = meta["shape"]
        names = [f"{name}_dim{i}" for i in range(len(shape))]
        times = length(f"{parent}/timestamps")
        rows = length(f"{parent}/id")
        regular = f"{parent}/starting_time" in arrays
        if shape and (times is not None and shape[0] == times or regular and name == "data"):
            names[0] = "time"
            if group_type == "ElectricalSeries" and name == "data" and len(shape) > 1:
                names[1] = "channel"
        elif shape and rows is not None and shape[0] == rows:
            names[0] = parent.rpartition("/")[2]
        if group_type == "ElectricalSeries" and name == "electrodes":
            names = ["channel"]
        meta["dimension_names"] = names
        refs[f"{path}/zarr.json"] = json.dumps(meta, separators=(",", ":"))
    return rfs


def write_nwb(rfs: dict, session, *, url_for: Callable[[str], str]) -> None:
    """Write the reference file system into a writable Icechunk session."""
    from shadowzarr.icechunk import rfs_to_icechunk

    rfs_to_icechunk(rfs, session, url_for=url_for)
