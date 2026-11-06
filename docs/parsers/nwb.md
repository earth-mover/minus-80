# NWB

What virtualizes cleanly in NWB (Neurodata Without Borders) files and what
does not. How to call the parser is in its
[README](../../src/minus80/parsers/nwb/README.md); traps shared with other
formats are in [formats.md](../formats.md).

The measurements below come from five DANDI files: three from dandiset
[000582](https://dandiarchive.org/dandiset/000582) (grid-cell ephys), one from
[000020](https://dandiarchive.org/dandiset/000020) and one from
[000228](https://dandiarchive.org/dandiset/000228) (Allen patch-seq icephys),
plus files written by pynwb in the tests.

## Let HDFParser read every dataset it can

VirtualiZarr's `HDFParser` reads variable-length strings, compound datasets
and datasets of object references. Pass `links_and_references=True`: without
it, a dataset of object references raises `NotImplementedError`, and every NWB
file has one (`electrodes/group` in the electrodes table). With it:

| Dataset | Stored as |
| --- | --- |
| numeric, any rank, scalars included | references into the file |
| variable-length string | inlined chunks, read at parse time |
| fixed-length string | references into the file |
| object reference | inlined chunks holding the target's path, relative to the array |
| compound, such as a `TimeSeriesReferenceVectorData` column | inlined chunks; a reference field holds a relative path |
| soft link | a `links_references` entry in the group's attributes, not a copy |

Across the five files, every dataset read back equal to h5py's decode: 489
numeric, 228 string and 5 object-reference datasets, and a compound column in
the pynwb test file. Object references and links follow the draft
[Links and Object References convention](https://github.com/catalystneuro/zarr-links-references),
which can still change.

Inlined chunks are copies, and `HDFParser` stores them uncompressed. Two
000582 files (7.49 MB) give a 352 KB repo, most of it the strings. The 000020
file holds 4.08 MB of strings in 125 arrays under `general/stimsets`, and with
the 000228 file (36.7 MB together) gives a 4.37 MB repo.

## What HDFParser still cannot read

- **Region references and variable-length sequences of numbers** raise
  `NotImplementedError`. None of the five files has either.
- **Compact-layout datasets** parse without error but have no chunk, so they
  read back as the fill value. HDF5 keeps a compact dataset's bytes in the
  object header, where a byte range cannot point. None of the five files has
  one.

Read these with h5py and store the values as JSON. `drop_variables` drops
names only in the group being parsed, never in its subgroups, so a dataset
that has to be dropped can only be dropped by parsing its group on its own.
The parser therefore calls `HDFParser` once per group, with that group's
subgroups dropped.

## Write large values as arrays, not attributes

A JSON attribute is parsed whole whenever the node is opened, and Icechunk
writes it as part of the node's metadata. On an Allen patch-seq file, one
icephys file took 12.9 s to write with its strings and stimulus templates as
one JSON attribute, and 1.2 s without them. As inlined arrays, the 000020 and
000228 files index and write in 1.2 s together.

## Index a remote file from a local copy

HDF5's chunk B-tree is scattered through the file, so walking it over the
network costs a round trip per node. Download the file, index the copy, and
record the remote URL in the manifest. On an 18 MB 000582 file read from S3,
indexing over the network took 3.48 s; downloading took 1.19 s and indexing
the copy 0.04 s.

## Leave unstated axis names null

NWB files attach no HDF5 dimension scales, so `HDFParser` names every axis
`phony_dim_<n>`. The parser writes those axes unnamed, since the file stated no
name. The NWB schema does name most axes (a 2-D `ElectricalSeries` `data` is
`(num_times, num_channels)`); `file_datatree` names the axes it can infer
from the schema, as described below.

## Make attributes strict JSON

NWB stores unused amplifier settings on `PatchClampSeries`
(`capacitance_slow`, `resistance_comp_*`) as NaN. Python's `json.dumps` writes
a bare `NaN`, which is not JSON, and Icechunk rejects the metadata (`bad
metadata ... expected value`). Non-finite floats become `null`. This was seen
on dandiset 000228, and applies to any HDF5 file.

An attribute holding an object reference, such as a `DynamicTableRegion`'s
`table`, becomes a ref convention object with the target's relative path.

## Compare decoded values by their bytes

Position tracking data contains NaN (12 values in one `SpatialSeriesLED1/data`
in 000582). `np.array_equal` is False on NaN even when the bytes are identical,
so the tests compare `tobytes()`.

## A contiguous dataset is one chunk

Some NWB numeric datasets are contiguous rather than HDF5-chunked. `HDFParser`
maps each to one chunk at the dataset's offset, which reads correctly but at
whole-array granularity.

## A space or `+` in a local directory

`Path.as_uri()` percent-encodes a space as `%20` and `+` as `%2B`, and DANDI
names files such as `..._behavior+ecephys.nwb`. The parser reads the file
through an open file handle, so indexing works whatever the path holds. Reading
the store back does not: for a source under a directory named `sp ace+plus`,
Icechunk finds no virtual chunk container for the chunk location, although the
container's prefix is the same encoded string. The cause is not yet known.

## Selectable axes, and what an index coordinate costs

A store you can open but not select on is half virtualized: the reader still
has to know NWB's sampling metadata, and cannot relate one array to another.
An index coordinate cannot stay virtual, because xarray builds a pandas index
over it. `file_datatree` materializes exactly these axes:

| Axis | Labels from | Cost, measured |
| --- | --- | --- |
| regular TimeSeries `time` | `starting_time` and its `rate` attribute | 2.88 M points: 23.0 MB to 103 KB |
| irregular TimeSeries `time` | the series' own `timestamps` | 12.5 M points: 99.6 MB to 2.4 MB |
| DynamicTable row axis | the table's `id` column | tens to thousands of ints |
| an axis labelled by a sibling column | that column, such as `electrodes` | one row per channel |

Set the compression explicitly. Zarr's default zstd compresses a computed ramp
6x and real timestamps not at all; blosc byte-shuffle with zstd compresses them
224x and 42x.

A `DynamicTableRegion` stores row positions, not ids: an `ElectricalSeries`'
`electrodes` column holds `0, 1, 2 …` while the electrodes table is indexed by
ids such as `850257411`. Left untranslated, two axes would share a name and
mean different things, and a `.sel` across them would return the wrong
channels. Resolve the `table` reference and write the ids.

A recorded clock is not always sorted. One probe in dandiset 000021 stamps
68,735 samples with `-1`, the rig's no-valid-time sentinel, a third of the way
through, so pandas refuses to slice its time axis by label. The values are
written as recorded, and the axis carries a `monotonic` attribute.

xarray refuses to rename a dimension to a name a variable already holds, and
that name only comes free once the virtual variable is dropped. Carry a
`<name>__idx` placeholder dimension until then.

A one-column table becomes nothing but its own index, which leaves a Dataset
with no virtual variables, and the writer warns about it. That warning is safe
to ignore.

## Attributes can be load-bearing

A regular TimeSeries has no `timestamps`. Its time axis is `starting_time` plus
`rate`, and NWB stores `rate` and `unit` as attributes of the scalar
`starting_time` dataset, so dataset attributes must travel with the
references. The same holds for units: 000582 stores microvolts and 000021
volts, each as a `conversion` factor on `data`.

## Typed read-back through hdmf-zarr

`NWBZarrIO` (hdmf-zarr) would let pynwb build typed `NWBFile` objects over a
Zarr store. hdmf-zarr 0.14.0 (2026-09-22) writes and reads Zarr v3 only, and
stores links in a `_LINKS` group attribute and dataset types in `_DTYPE` and
`_REFERENCE_FIELDS`
([changelog](https://github.com/hdmf-dev/hdmf-zarr/blob/dev/CHANGELOG.md)).
`HDFParser` writes the draft Links and Object References convention instead,
so `NWBZarrIO` is not expected to read a store from this parser. Reading one
with it has not been tried.
