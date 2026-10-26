# minus80.ome

Build the OME-Zarr metadata that describes a set of virtual Zarr arrays as
images, and check a finished store against it. The arrays themselves are
written elsewhere, by VirtualiZarr; this package returns attribute dictionaries
and never reads or writes pixels, except in the `levels aligned` gate of
`verify`.

There is no extra to install. The documents are built with
[ome-zarr-models](https://github.com/ome-zarr-models/ome-zarr-models-py) and
validated against the JSON schemas vendored in
[ngff-zarr](https://github.com/thewtex/ngff-zarr), both dependencies of
`minus-80`.

## Modules

| Module | What it does |
| --- | --- |
| `image` | `image_attrs` builds the value of the `ome` key for one multiscale image: axes, units, per-level scale and optional translation, and an optional `omero` display block from `rgb_omero` or `channel_omero`. Each axis's type comes from the array's `dimensions` attribute, passed as `dimensions=`; without it only `t`, `time`, `c`, `channel`, `z`, `y` and `x` are typed, and any other axis name raises. `label_attrs` and `labels_attrs` do the same for label images. Every document is validated before it is returned, including the axis rules the JSON schema leaves out: 2 or 3 space axes, at most one time axis and at most one channel or custom-typed axis, in that order. |
| `collection` | `root_attrs` and `series_attrs` build the `bioformats2raw.layout` container: the root group's attributes and the `OME` subgroup listing the member images by path. |
| `annotations` | `image_annotation` puts per-image fields under a top-level `annotations` key, outside `ome`. `annotation_table` builds a column-oriented table of every image's fields for the collection root. |
| `hierarchy` | `series`, `images` and `multiscale_groups` find the images in a collection, following nested `series` paths and skipping `labels` subgroups. |
| `verify` | `GATES` holds checks every store of images should pass: schema validity, dimension names, plausible pixel sizes, `series` matching the groups present, level order and alignment, and a `source_url` on every virtual array. `run_gates` runs a table of them and returns the number that failed. |

## Usage

```python
from minus80.ome import image_annotation, image_attrs, rgb_omero, root_attrs, series_attrs

root.attrs.update(root_attrs())
root["OME"].attrs.update(series_attrs(["slide-01"]))
root["slide-01"].attrs.update(
    {"ome": image_attrs(name="slide-01", dims=("c", "y", "x"),
                        level_shapes=[(3, 26681, 40000), (3, 13340, 20000)],
                        scale={"c": 1.0, "y": 0.25, "x": 0.25},
                        units={"y": "micrometer", "x": "micrometer"},
                        omero=rgb_omero())}
    | image_annotation({"stain": "H&E"}, source_url=url))
```

Each level's scale is derived from its shape relative to level 0, so a level a
scanner rounded to 13340 rows gets its true ratio rather than exactly 2.

## Versions

Output is OME-Zarr 0.5 by default. Pass `version="0.6"` for the 0.6 layout,
where a multiscale's axes sit inside a named `coordinateSystems` entry and each
level's transform names its `input` and `output`.

A 0.6 document declares `"version": "0.6"`. Some ngff-zarr releases vendor a
0.6 schema that accepts only `"0.6rc0"`, so `validate` checks the declared
string against the published schema's list and validates the rest of the
document under whichever spelling ngff-zarr knows (`V06_ONDISK_VERSION`).

## Limits

- Axis types are inferred from axis names: `t` is time, `c` is channel,
  `image` is custom, and every other name is space.
- `label_attrs` validates against the strict schema only when `colors` is
  given, because the strict variant requires a colour per label.
- `verify.schema` calls ngff-zarr's validator directly, without the version
  handling in `image.validate`.
- `verify.provenance` can only check an Icechunk store.
