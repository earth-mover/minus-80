# minus80.core

Shared code for turning a file's bytes into Icechunk chunk references. Parsers
and ingests call it; nothing in it is specific to one file format or one
archive. For a format VirtualiZarr already parses (HDF5, NetCDF, TIFF, Zarr,
GRIB), call VirtualiZarr's parser directly instead.

There is no extra to install: the dependencies of `minus-80` cover every module
except the two optional code paths listed under [Limits](#limits).

## Modules

| Module | What it does |
| --- | --- |
| `registry` | `as_url` turns a local path into a `file://` URL. `store_and_key` resolves a URL to an obstore store and the key within it, undoing percent-encoding. `registry_for` builds an `ObjectStoreRegistry` for a file's directory. |
| `fetch` | `fetch_head` reads the first bytes of a remote file. `download_then_index` copies a whole file into a local cache with parallel range GETs. `sparse_local` writes only the byte ranges you name into a sparse local file. |
| `remote` | `reader` and `buffered` give a parser a seekable, block-cached file object over a remote file, with long timeouts and a slow retry backoff. `downloaded` returns an opener that fetches the whole file first. |
| `builders.flat_binary` | `flat_manifest_array` builds a `ManifestArray` for an uncompressed, C-ordered array from its shape, dtype, byte order and data offset. Each chunk is one slab along one axis; `stride` handles a format that puts a header before each slab. |
| `icechunk_io` | `create_repo` creates or reopens a repo with one virtual chunk container per source prefix. `write_manifest_group` writes a `ManifestGroup` and commits. `open_repo` and `open_repository` reopen a repo and authorize the prefixes stored in its config. `commit` rebases onto commits that landed first. `ensure_repo_access` adds a virtual chunk container to an Arraylake repo. |
| `multiscale` | `write_multiscale` writes an OME-Zarr 0.5 image whose full-resolution level is virtual and whose downsampled levels are computed by ngff-zarr and stored as real chunks. |
| `decisions` | `Decision` records a value or interpretation that the source files do not state, with the evidence for it, and renders it as a sentence or as a record. |
| `progress` | `Progress` prints a status line at a fixed time interval while a long job runs. |
| `bench` | `compare_remote` times reading a collection through a virtual repo against downloading each file and reading it with the format's own library, at several injected network latencies, each read in a fresh subprocess. `summarize` reduces the results to medians. |

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
  `VIRTUAL_CORE_S3_ANONYMOUS=1` to read a public bucket unsigned.
- `open_repo(coalesce=True)` imports `coalescing_zarr`, and
  `bench.compare_remote` imports an HTTP range server that injects latency.
  Neither package is a dependency of `minus-80`.
- `inspect.directory_layout` judges BigTIFF files only. It reports a classic
  TIFF as not bulk-readable.

Format pitfalls are collected in [docs/formats.md](../../../docs/formats.md),
and the parts of this package that belong upstream in
[docs/upstream.md](../../../docs/upstream.md).
