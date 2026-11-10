# minus80.core

Shared code for turning a file's bytes into Icechunk chunk references. Parsers
and ingests call it; nothing in it is specific to one file format or one
archive. For a format VirtualiZarr already parses (HDF5, NetCDF, TIFF, Zarr,
GRIB), call VirtualiZarr's parser directly instead.

There is no extra to install: the dependencies of `minus-80` cover every module.

## Modules

| Module | What it does |
| --- | --- |
| `registry` | `as_url` turns a local path into a `file://` URL, and `file_path` turns a `file://` URL back into a decoded local path. `store_and_key` resolves a URL to an obstore store and the key within it, undoing percent-encoding. `registry_for` builds an `ObjectStoreRegistry` for a file's directory. `resolve` finds a URL's store and decoded key in a registry the caller built, for a parser that must read through it. Both take `patient=True` for an index walk: a 300 s timeout and a retry backoff over 20 minutes, where the default fails fast. |
| `fetch` | `read_range` and `object_size` read one range or one length. `list_objects` maps every non-empty object under a prefix to its size. `iter_ranges` and `write_ranges` fetch many ranges, one GET per block with a bounded number in flight, and raise on a short read. `download_then_index` copies a whole file into `cache_dir()`; `sparse_local` writes only the byte ranges you name into a sparse local file; `local_copy` returns a local path for any source. Each reads through a `registry` when one is passed. |
| `remote` | `reader` gives a parser a seekable, block-cached file object over a remote file, read through the caller's registry or the patient store. |
| `manifest` | `manifest_array` builds a `ManifestArray` from a grid of paths, offsets and lengths: zero-length or path-less chunks become missing, attributes are made JSON-safe, and `source_url` is set from the paths. `channel_last_transpose` is the codec for channel-interleaved bytes declared channel-first. `stack`, `concatenate` and `permute_dims` combine and reorder arrays by dimension name, keeping `source_url`, `dimensions` and the other attributes true to the result. `with_lengths` replaces every chunk reference's length and keeps the rest. |
| `builders.flat_binary` | `flat_manifest_array` builds a `ManifestArray` for an uncompressed, C-ordered array from its shape, dtype, byte order and data offset. Each chunk is one slab along one axis; `stride` handles a format that puts a header before each slab. |
| `icechunk_io` | `create_repo` creates or reopens a repo with one virtual chunk container per source prefix. A location may be a directory, an `s3://` URL or `arraylake://<org>/<repo>`; `create_repo`, `open_repository` and `open_repo` send the last to `arraylake_repository`, which opens an existing Arraylake repo and never creates one, and `storage_for` raises for it. `write_manifest_group` writes a `ManifestGroup` and commits. `open_repo` and `open_repository` reopen a repo and authorize the prefixes stored in its config. `commit` rebases onto commits that landed first. |
| `multiscale` | `write_multiscale` writes an OME-Zarr 0.5 image whose full-resolution level is virtual and whose downsampled levels are computed by ngff-zarr and stored as real chunks. |
| `decisions` | `Decision` records a value or interpretation that the source files do not state, with the evidence for it, and renders it as a sentence or as a record. |
| `progress` | `Progress` prints a status line at a fixed time interval while a long job runs. |
| `jsonable` | `jsonable` turns a value into strict JSON for a Zarr attribute: NaN becomes `null`, bytes become text or `{"base64": ...}`. |
| `aio` | `run_sync` runs a coroutine from synchronous code, including inside a running event loop such as Jupyter's; `submit` starts one on the same loop and returns a cancellable future. |

## Usage

Virtualize a headerless `uint16` volume and read one plane back through the
repo:

```python
from virtualizarr.manifests import ManifestGroup

from minus80.core import as_url
from minus80.core.builders.flat_binary import flat_manifest_array
from minus80.core.icechunk_io import open_repo, write_manifest_group

url = as_url("volume.raw")
volume = flat_manifest_array(url, shape=(4, 3, 2), dtype="uint16", data_offset=0,
                             endian="little", dimension_names=("z", "y", "x"))
write_manifest_group(ManifestGroup(arrays={"volume": volume}), "repo",
                     source_urls=[url])
open_repo("repo")["volume"][1]
```

`endian` has no default: read it from the file's header.

## Limits

- `create_repo` builds virtual chunk containers for local, `s3://` and
  `http(s)://` sources only. Any other scheme raises `ValueError`.
- `s3://` sources are read with credentials from the environment. Set
  `MINUS80_S3_ANONYMOUS=1` to read a public bucket unsigned.
- An S3 bucket's region is `AWS_REGION` or `AWS_DEFAULT_REGION` when set.
  Otherwise it is asked of S3 once per bucket, with an unsigned `HEAD` to
  `s3.amazonaws.com`. When `AWS_ENDPOINT_URL` or `AWS_ENDPOINT` is set, or the
  lookup fails, the region is left to obstore and icechunk, which assume
  us-east-1.

Format pitfalls are collected in [docs/formats.md](../../../docs/formats.md),
and the parts of this package that belong upstream in
[docs/upstream.md](../../../docs/upstream.md).
