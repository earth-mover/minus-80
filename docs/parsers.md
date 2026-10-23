# Parsers

This page covers what a parser in `minus80.parsers` takes and returns, where it
puts each fact a file states, and how an ingest combines the arrays that
parsers return. Which formats can be virtualized at all is in
[formats.md](formats.md).

## The interface

### Write one parser per format, on VirtualiZarr's protocol

Each format gets one class, `<Format>Parser`, that follows VirtualiZarr's
`Parser` protocol
([`parsers/typing.py`](https://github.com/ianhi/VirtualiZarr/blob/83da047711558dfb4fc004db45641d28a6687f61/virtualizarr/parsers/typing.py#L14-L20)):

```python
def __call__(self, url: str, registry: ObjectStoreRegistry) -> ManifestStore: ...
```

The constructor takes the choices the file does not fix, such as which TIFF
series to read or how to fetch the directories. The call takes one file. With
one signature for every format, an ingest calls a minus80 parser and one of
VirtualiZarr's own parsers the same way, and a minus80 parser can move into
VirtualiZarr unchanged.

```python
from minus80.core import registry_for
from minus80.parsers.tiff import TiffParser

store = TiffParser(series=0)(url, registry_for(url))
store.group["0"]   # the full-resolution ManifestArray
```

### Read through the registry the caller passes

The registry maps URL prefixes to configured object stores: the bucket, its
region, credentials, timeouts and retries. Only the caller knows those, and
obstore applies a store's settings only when the store is created, so a parser
cannot add them later. Resolve the URL with
`minus80.core.registry.resolve(registry, url)` and read through the store it
returns. `registry_for(url)` builds a registry for a public source, and
`registry_for(url, patient=True)` builds one for a walk of thousands of
sequential reads.

### Read each file once

The call reads everything an ingest needs from the file: the chunk references
and every fact the plan and the OME-Zarr metadata need. An ingest that reopens
a file with the format's library to read one more value is asking the parser
for a field it should already return.

### Refuse with the reason

When the parser cannot virtualize a file, it raises an error that names why: a
compression no Zarr codec decodes, a strip layout with no regular chunk grid. A
run over thousands of files catches the error, records the reason and carries
on. A parser never writes references that would decode wrong.

## Where facts go

### Put what the file states in JSON attributes

Every fact the file states goes in JSON attributes, on the file's group or on
the array the fact describes: pixel sizes, channel names, acquisition
metadata, a TIFF's resolution tags. Attributes reach the store with the
arrays, so a reader of the store sees them. Keep the source's own names and
values; convert a unit into the attribute OME-Zarr reads (`scale`, `units`)
beside the stated value, not in place of it.

An array's attributes use these keys for physical metadata, each keyed by
dimension name: `scale` (the size of one element), `units` (an OME-Zarr unit
name such as `"micrometer"`), `translation`, and `dimensions` (below).
`channel_names` is a list in channel-axis order. `manifest_array` raises when a
key of one of these mappings is not a dimension of the array.

### State each axis's type in `dimensions`

Each array carries a `dimensions` attribute that states what every named axis
measures:

```python
{"z": {"type": "space"}, "c": {"type": "channel"}, "y": {"type": "space"}, "x": {"type": "space"}}
```

A type is `space`, `time` or `channel`, the three OME-Zarr 0.5 defines, or
another string for an axis those do not describe, such as `fov` for a stack of
fields of view. `minus80.ome` writes each axis's type from `dimensions`, so a
parser that knows an axis is time says so, and nothing downstream guesses it
from the name. Leave an axis out when the file does not say what it is.

An OME-Zarr image allows one axis that is either `channel` or a custom type,
not both, so a stack of fields of view with a channel axis, `(fov, c, z, y, x)`,
is not an image: `minus80.ome` refuses it. Keep each field of view an image of
its own, or give the stack no channel axis, as in `(fov, z, y, x)`.

## Composing

### Combine arrays through `minus80.core.manifest`

An ingest builds bigger arrays from parsed ones with the wrappers in
`minus80.core.manifest`, not by calling `np.stack` or `np.concatenate` on
`ManifestArray`s directly:

- `stack(arrays, dim=, axis=)` adds a named axis and its `dimensions` entry;
- `concatenate(arrays, dim=)` joins along a named axis;
- `permute_dims(array, order)` reorders axes, for a source stored `(z, c, y, x)`
  that OME-Zarr wants as `(c, z, y, x)`.

VirtualiZarr's `np.stack` and `np.concatenate` merge the chunk manifests
correctly but take every attribute from the first array, so `source_url`
names one file of the several the manifest references, and the stacked axis
has no name. The wrappers set `source_url` from the merged manifest, keep
`dimensions`, `scale`, `units` and `translation` consistent with the new axes,
and raise when any other attribute differs between the inputs, such as a z
spacing of 0.5 µm in one file and 0.2 µm in another.

`permute_dims` reorders the chunk grid and, when the reordering changes the
order of axes inside a chunk, prepends Zarr's `transpose` codec, so every
chunk still points at the same stored bytes.
