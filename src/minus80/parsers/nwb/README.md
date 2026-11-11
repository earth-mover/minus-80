# minus80.parsers.nwb

NWB (Neurodata Without Borders) files as VirtualiZarr references, with every
dataset and link in the file carried into the store.

```bash
uv sync --extra nwb
```

NWB is HDF5, so the parsing is VirtualiZarr's `HDFParser`. Numeric datasets
become references into the file. Strings, compound tables and object
references are read at parse time and stored in the repo, and soft links are
recorded as links rather than copied. The few datasets `HDFParser` cannot
represent are read with h5py into the `nwb_native` attribute of their group.

```python
from minus80.core import icechunk_io
from minus80.parsers.nwb import open_collection, file_datatree

group = open_collection(["a.nwb", "s3://bucket/blob"], names=["a", "sub-01/ses-1"])
icechunk_io.write_manifest_group(group, "repo", source_urls=[...])

tree = file_datatree("a.nwb")
tree.vz.to_icechunk(session.store)
```

| Function | What it returns |
| --- | --- |
| `open_collection(sources, *, urls=None, names=None, attributes=None)` | One `ManifestGroup` with a group per file, mirroring the file's HDF5 hierarchy. Axes are unnamed, because NWB files state no names. |
| `file_datatree(source, *, url=None, attributes=None)` | One file as an `xr.DataTree`, a node per group, with sibling datasets on shared, indexed axes. |
| `virtual_arrays(group)` | Every array in an opened repo, keyed `<file>/<nwb path>`. |

A source is a path, a URL or an open binary file. `url=` (`urls=` for a
collection) is the address the manifest records; an open file has no address
of its own, so it needs one. A remote source is downloaded to
`minus80.core.fetch.cache_dir()` and indexed from the copy. `names` defaults to
each file's stem; for a content-addressed blob URL, pass the archive's asset
path instead.

`open_collection` returns a `ManifestGroup` rather than arrays, because its
output is a hierarchy of files and NWB paths.

`file_datatree` makes the store selectable without knowing NWB:

- A TimeSeries gets a `time` axis in seconds, from its `timestamps` or computed
  from `starting_time` and `rate`. The axis carries a `monotonic` attribute,
  because recorded clocks are not always sorted.
- A DynamicTable's columns share one row axis, indexed by the table's `id`.
- A 1-D sibling whose length matches another array's non-first axis labels that
  axis. An `ElectricalSeries`' `electrodes` column stores row positions, so it
  is resolved to the ids of the electrodes table.

## Limits

- **Strings are copied, uncompressed.** `HDFParser` stores each string,
  compound and object-reference dataset in the repo as plain bytes. A file with
  large text, such as Allen icephys `general/stimsets`, gives a repo around a
  tenth of the file's size.
- **Object references and links follow a draft convention**, VirtualiZarr's
  `links_and_references` option, which can still change.
- **Region references, numeric variable-length sequences and compact-layout
  datasets** are stored as JSON in `nwb_native`.
- **A contiguous dataset is one chunk**, so it reads at whole-array granularity.
- **A space or `+` in a local directory breaks reading back.** Indexing works,
  but Icechunk does not match the encoded chunk location to its container.
- **Time axes over 50 M samples get no coordinate** (`MAX_COORD_POINTS`); the
  `timestamps`, or `starting_time` with its `rate`, stay as variables.

[docs/parsers/nwb.md](../../../../docs/parsers/nwb.md) has the measurements
behind these.
