# minus80.parsers.nwb

Reference the arrays in NWB (Neurodata Without Borders) files from Icechunk
without copying them. NWB is HDF5, so the parsing is VirtualiZarr's
`HDFParser`; this package decides which datasets can be referenced, carries the
rest natively, and maps NWB's schema onto named, indexed axes.

```bash
uv sync --extra nwb
```

## Two layouts

| Function | Output | Axes |
| --- | --- | --- |
| `open_collection(sources, names=None)` | one `ManifestGroup`, a group per file at `<name>/<nwb path>` | unnamed; every array keeps its own |
| `file_datasets(source)` | `{parent_path: xr.Dataset}` for one file, plus its native metadata | shared and indexed: `time`, table row ids, electrode ids |

```python
from minus80.core import icechunk_io
from minus80.parsers.nwb import open_collection, file_datasets

group = open_collection(["a.nwb", "b.nwb"])
icechunk_io.write_manifest_group(group, "repo", source_urls=[...])

datasets, nwb_attrs = file_datasets("a.nwb")
for parent in sorted(datasets, key=lambda p: p.count("/")):
    datasets[parent].vz.to_icechunk(session.store, group=f"a/{parent}")
```

`open_collection` writes the hierarchy as it is, one array per numeric dataset.
NWB files carry no HDF5 dimension scales, so the arrays have no dimension names.

`file_datasets` builds one Dataset per NWB parent group, with sibling arrays
(`data`, `timestamps`) as variables on shared axes, so `.sel(time=…)` and
`.sel(units=…)` work without knowing NWB:

- A TimeSeries gets a `time` axis in seconds, taken from its `timestamps` or
  computed from `starting_time` and `rate`. The axis carries a `monotonic`
  attribute, because recorded clocks are not always sorted.
- A DynamicTable's columns share one row axis, indexed by the table's `id`.
- A 1-D sibling whose length matches another array's non-first axis labels that
  axis. A `DynamicTableRegion` (an `ElectricalSeries`' `electrodes`) stores row
  positions, so it is resolved to the ids of the table it points at.

Write each parent shallowest first: some NWB groups hold data and are also
ancestors of other data groups.

## What is bought

| Job | Library |
| --- | --- |
| Chunk references into HDF5 | VirtualiZarr `HDFParser` |
| Walking the tree, classifying dtypes, reading native values | h5py |
| Per-parent Datasets and index coordinates | xarray |
| Reference reader in the tests | pynwb |

## What is referenced and what is copied

| Dataset kind | Example | Handling |
| --- | --- | --- |
| Numeric N-D arrays | `ElectricalSeries/data`, `units/spike_times`, `electrodes/id` | referenced, chunked or contiguous |
| Variable-length strings | `session_description`, `general/subject/*`, `specifications/` | native |
| Compound dtypes | `intervals` records | native |
| HDF5 object references | `DynamicTableRegion.table`, `electrodes/group` | native, as strings |
| Scalars | `starting_time` | native, with the dataset's own attributes |

Native values and every group's attributes travel in an `_nwb` attribute,
keyed by file, so a store opens and reads without the source files. Index
coordinates in `file_datasets` are real bytes too, compressed with blosc
byte-shuffle and zstd.

## Limits

- **Native data is stored as JSON attributes.** Large native content (stimulus
  templates, the embedded spec cache) makes writes slow. `open_collection` puts
  every file's native data in one root attribute, so the cost grows with the
  collection.
- **Contiguous datasets are one chunk.** A multi-GB contiguous array references
  fine but reads at whole-array granularity.
- **Remote sources are downloaded once to index.** HDF5's chunk B-tree is
  scattered through the file, so a remote file is fetched to
  `minus80.core.fetch.cache_dir()` first; the store still references the
  remote URL.
- **A `+`, a space or another percent-encoded character in a local path breaks
  indexing,** in the file name or in any directory above it. `HDFParser` reads
  through obspec-utils' `ObjectStoreRegistry`, which neither decodes the key
  nor matches an encoded path against a decoded store prefix
  ([docs/upstream.md](../../../../docs/upstream.md)).
- **Time axes over 50 M samples get no coordinate.** Past `MAX_COORD_POINTS`,
  `timestamps` stays a referenced variable, and a regular series' `starting_time`
  and `rate` are only in `_nwb`.

[FINDINGS.md](FINDINGS.md) has the detail behind each of these.
