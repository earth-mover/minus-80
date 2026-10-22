# Virtualizing NWB: findings

What virtualizes cleanly in NWB (Neurodata Without Borders) and what fights
back. The cross-format traps in [docs/formats.md](../../../../docs/formats.md)
(heap-backed strings, phony-dim collisions) all apply here.

Measured on DANDI dandiset [000582](https://dandiarchive.org/dandiset/000582)
(grid-cell ephys, CC-BY-4.0), two ~3.7 MB `behavior+ecephys` files: 7.49 MB of
sources became a 63.6 KB repo, and all 20 numeric arrays read back bitwise
equal to h5py's own decode.

## The parser chokes on heap datasets

Calling `HDFParser` on a group that contains a variable-length string dataset
raises (`'bytes'`/`Reference` has no `.item()`). The parser classifies with h5py
first, then calls `HDFParser` once per parent group with `drop_variables`
naming every non-numeric sibling and subgroup, so it only builds the arrays
asked for. Every HDF5-family format (h5ad, NWB, 10x `.h5`) needs the same
classify-and-drop step; a numeric-leaves-only mode in `HDFParser` would remove
it.

## Attributes that are not JSON

- **HDF5 object references.** `DynamicTableRegion` columns (`electrodes`,
  `spike_times_index`) carry a `table` attribute that is an object reference,
  and zarr's metadata write raises `TypeError: Object of type Reference is not
  JSON serializable`. Every array and group attribute is sanitized,
  stringifying anything non-serializable.
- **NaN scalars.** NWB stores unused amplifier settings on `PatchClampSeries`
  (`capacitance_slow`, `resistance_comp_*`) as NaN 0-D floats. Python's
  `json.dumps` emits a bare `NaN`, which is not valid JSON, and Icechunk's
  parser rejects it (`bad metadata ... expected value`). Non-finite floats become
  `null`. Seen on Allen dandiset 000228; this is an HDF5 trap, not an NWB one.

## No invented axis names

NWB files attach no HDF5 dimension scales, so `HDFParser` names every axis
`phony_dim_<n>`. `open_collection` nulls those, so arrays are written unnamed
rather than under names the file never stated. The NWB schema does name most
axes (a 2-D `ElectricalSeries` `data` is `(num_times, num_channels)`); taking
names from the schema is not done. Siblings in that layout share no axis, so
the store does not open with `xr.open_datatree`.

## NaN is not a mismatch

Position tracking data legitimately contains NaN (12 in one
`SpatialSeriesLED1/data`). `np.array_equal` is False on NaN while the bytes are
identical, so the tests compare `tobytes()`.

## Native metadata dominates the write

On Allen patch-seq files, one icephys file writes in 12.9 s, or 1.2 s with the
native blob stripped. Four files take ~288 s, because `open_collection` puts
every file's native data (object-dtype stimulus templates, the embedded
`/specifications/` cache, per-sweep scalars) into one root `_nwb` attribute
that grows with the collection. The virtual arrays are cheap: 2,249 ophys
arrays wrote in 30 s. The fix is to write large native data as native Zarr
arrays, or per-file group attributes, instead of one root attribute.

## Contiguous datasets are one chunk

Some NWB numeric datasets are contiguous rather than HDF5-chunked. `HDFParser`
maps each to a single chunk at the dataset's storage offset, which references
correctly but reads at whole-array granularity.

## Local paths with `+` or spaces

DANDI paths such as `..._behavior+ecephys.nwb` become `%2B` through
`Path.as_uri()`. `HDFParser` resolves that URL through obspec-utils'
`ObjectStoreRegistry`, which returns the key still encoded, so `a+b.nwb` is
looked up as `a%2Bb.nwb`, and which strips the store's decoded prefix from the
encoded path, so a file under `sp ace/` or `pl+us/` gets its whole path as the
key. Both raise `FileNotFoundError`. `minus80.core.registry.store_and_key`
works around both for every fetch, but `HDFParser` calls `resolve` itself.
Name local copies without them until obspec-utils decodes the path.

## Selectable axes: what an index coordinate costs

A store you can open but not select on is half virtualized: the reader still
has to know NWB's sampling metadata and still cannot relate one array to
another. An index coordinate cannot stay virtual, because xarray builds a
pandas index over it (VirtualiZarr's `open_virtual_dataset` loads dimension
coordinates for the same reason). `datasets.py` materializes exactly the axes:

| Axis | Labels from | Cost, measured |
| --- | --- | --- |
| regular TimeSeries `time` | `starting_time` + the `rate` attribute | 2.88 M points: 23.0 MB to 103 KB |
| irregular TimeSeries `time` | the series' own `timestamps` | 12.5 M points: 99.6 MB to 2.4 MB |
| DynamicTable row axis | the table's `id` column | tens to thousands of ints |
| an axis labelled by a sibling column | that column, e.g. `electrodes` | one row per channel |

The compression is set explicitly. Zarr's default zstd gets 6x on a computed
ramp and 1x on real timestamps; blosc byte-shuffle plus zstd gets 224x and 42x.

`DynamicTableRegion` stores row positions, not ids: an `ElectricalSeries`'
`electrodes` column holds `0, 1, 2 …` while the electrodes table is indexed by
ids like `850257411`. Left untranslated, two axes would share a name and mean
different things, and a `.sel` across them would return the wrong channels.
`_index_values` resolves the `table` reference and writes the ids.

Two mechanical traps:

- xarray refuses to rename a dim to a name a variable already holds, and that
  name only comes free once the virtual variable is dropped. A `<name>__idx`
  placeholder dim is carried until `_materialize` runs.
- A one-column table becomes nothing but its own index, which leaves a Dataset
  with no virtual variables, and the writer warns about duplicating data. That
  one warning is safe to suppress.

A recorded clock is not always sorted. One DANDI 000021 probe stamps 68,735
samples with `-1`, the rig's no-valid-time sentinel, a third of the way
through, so pandas refuses to slice its time axis by label. The values are
written as recorded and the axis carries a `monotonic` attribute.

## An attribute can be load-bearing

A regular TimeSeries has no `timestamps`. Its axis is `starting_time` plus a
`rate`, and NWB stores `rate` (and `unit`) as attributes of the scalar
`starting_time` dataset. A native entry is therefore `{"value", "attrs"}` when
the dataset has attributes and a bare value otherwise; native values are never
dicts, so `native_parts` splits either form.

The same holds for array attributes. Dandiset 000582 stores microvolts and
000021 volts, recorded as a `conversion` factor on the data; comparing the two
is only meaningful because those attributes travel with the references.

## Typed read-back through hdmf-zarr (not done)

`NWBZarrIO` (hdmf-zarr) copies bytes, so it never replaces `HDFParser`. Its
NWB-to-Zarr conventions ([storage spec](https://hdmf-zarr.readthedocs.io/en/stable/storage.html))
cover what the `_nwb` attribute does by hand: object references as
`zarr_dtype: "object"`, links as a `zarr_link` group attribute, compound dtypes
as a `zarr_dtype` list, variable-length strings as Zarr unicode. A store written
to those conventions, with numeric arrays as references and the rest as native
Zarr, would let pynwb rebuild typed `NWBFile` objects over it.

Two blockers: hdmf-zarr is Zarr v2 while Icechunk is Zarr v3
([hdmf-zarr#202](https://github.com/hdmf-dev/hdmf-zarr/issues/202)), and HDF5
filters other than gzip need per-filter checking against Zarr codecs.
