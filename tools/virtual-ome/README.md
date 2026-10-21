# virtual-ome

Turn a set of virtual Zarr arrays into an OME-Zarr hierarchy a viewer can open.

Byte-range references make a study's pixels reachable. They do not make it
usable: a root group holding 205 arrays is a directory listing with extra steps.
This package supplies the metadata that closes that gap — which axes an array
has, what one pixel measures, which arrays are resolution levels of each other,
which groups are images, and what each image is of.

| Module | What it produces |
| --- | --- |
| `image` | The `ome` attributes for one multiscale image, validated against the spec schema. |
| `collection` | The `bioformats2raw.layout` container for a group of independent images. |
| `annotations` | Per-image fields, plus a searchable table of them on the collection. |

Nothing here touches pixels, and nothing writes JSON by hand — the metadata
classes and the validator both come from [ngff-zarr](https://github.com/thewtex/ngff-zarr).

## Version

Output targets OME-Zarr **0.6**, where a multiscale image's axes live inside a
named `coordinateSystems` entry and each level's transform names its `input` and
`output`.

One caveat: ngff-zarr 0.46 ships a release-candidate schema that accepts only
`"0.6rc0"` as the version string and rejects a plain `"0.6"`, so that is what
gets written. See `virtual-idr/LIMITS.md`.
