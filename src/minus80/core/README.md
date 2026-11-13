# minus80.core

Shared code for turning a file's bytes into Icechunk chunk references. Parsers
and ingests call it; nothing in it is specific to one file format or one
archive. For a format VirtualiZarr already parses (HDF5, NetCDF, TIFF, Zarr,
GRIB), call VirtualiZarr's parser directly instead.

The dependencies of `minus-80` cover every module except two. `pyramid` needs the `tiff` extra. `tables` needs the `tables` extra.

## Modules

| Module | What it does |
| --- | --- |
| `registry` | `as_url` turns a local path into a `file://` URL, and `file_path` turns a `file://` URL back into a decoded local path. `store_and_key` resolves a URL to an obstore store and the key within it, undoing percent-encoding. `registry_for` builds an `ObjectStoreRegistry` for a file's directory. `resolve` finds a URL's store and decoded key in a registry the caller built, for a parser that must read through it. Both take `patient=True` for an index walk: a 300 s timeout and a retry backoff over 20 minutes, where the default fails fast. |
| `fetch` | `read_range` and `object_size` read one range or one length. `iter_ranges` and `write_ranges` fetch many ranges, one GET per block with a bounded number in flight, and raise on a short read. `download_then_index` copies a whole file into `cache_dir()`; `sparse_local` writes only the byte ranges you name into a sparse local file; `local_copy` returns a local path for any source. Each reads through a `registry` when one is passed. |
| `remote` | `reader` gives a parser a seekable, block-cached file object over a remote file, read through the caller's registry or the patient store. `counting_reader` also returns a `ReadCounter` of the bytes and requests of every read. The counter also holds the progress line that those bytes advance. |
| `elements` | `ElementWriter` writes an element in units, one commit per unit. `has` says whether the branch holds a unit. So a rerun skips that unit. `finish` sets `COMPLETE` (`minus80:complete`) on the element's group. `complete` reads it. `ElementWriter` refuses a branch in `protected`. |
| `manifest` | `manifest_array` builds a `ManifestArray` from a grid of paths, offsets and lengths: zero-length or path-less chunks become missing, attributes are made JSON-safe, and `source_url` is set from the paths. `channel_last_transpose` is the codec for channel-interleaved bytes declared channel-first. `stack`, `concatenate` and `permute_dims` combine and reorder arrays by dimension name, keeping `source_url`, `dimensions` and the other attributes true to the result. |
| `builders.flat_binary` | `flat_manifest_array` builds a `ManifestArray` for an uncompressed, C-ordered array from its shape, dtype, byte order and data offset. Each chunk is one slab along one axis; `stride` handles a format that puts a header before each slab. |
| `icechunk_io` | `create_repo` creates or reopens a repo with one virtual chunk container per source prefix. `write_manifest_group` writes a `ManifestGroup` and commits. `open_repo` and `open_repository` reopen a repo and authorize the prefixes stored in its config. `commit` rebases onto commits that landed first. |
| `multiscale` | `write_multiscale` writes an OME-Zarr 0.5 image whose full-resolution level is virtual and whose downsampled levels are computed by ngff-zarr and stored as real chunks. |
| `pyramid` | Needs the `tiff` extra. `create_level` makes a blosc-zstd array with one chunk per tile. It can shard the array along z. `write_page` decodes each tile of a TIFF page once with tifffile and writes it. It puts the samples on the axis before `y`. `check_page` compares named tiles with the array. `channel_names` reads the channel names of an OME-XML string. |
| `decisions` | `Decision` records a value or interpretation that the source files do not state, with the evidence for it, and renders it as a sentence or as a record. |
| `blocks` | `compare_blocks` compares two readers of the same bytes by SHA-256. It compares the first block, the last block and a seeded sample between. It returns the sampled and the differing block indices. It does not find a difference in another block. |
| `progress` | `Progress` prints a status line at a fixed time interval while a long job runs. |
| `jsonable` | `jsonable` turns a value into strict JSON for a Zarr attribute: NaN becomes `null`, bytes become text or `{"base64": ...}`. |
| `aio` | `run_sync` runs a coroutine from synchronous code, including inside a running event loop such as Jupyter's; `submit` starts one on the same loop and returns a cancellable future. |
| `zarr_copy` | `copy_group` copies every Zarr v2 array in a zip, and the attributes of every group, into a v3 group. `copy_chunks` writes one array's chunk members to their v3 keys, bytes unchanged. `create_copy` makes the v3 array from a `.zarray`. It translates blosc, Fortran order and `vlen-utf8`. Any other filter or compressor raises `NotImplementedError`. `chunk_members` lists the chunk members of one v2 array, under either separator. `chunk_key` maps a v2 chunk member to its v3 chunk key. `read_array` decodes a whole numeric v2 array from an open zip into NumPy. `read_rows` decodes a range of rows. `read_region` decodes one range for each leading axis. Each reads only the chunks it touches. |
| `tables` | Needs the `tables` extra. `write_parquet` writes a Parquet file with pandas index and `geo` footer metadata. `TableWriter` writes Parquet files into the tables of one namespace and registers them. The file name holds the hash of its bytes. `TableWriter` renames a local file into place. A remote object of the wrong size raises `ValueError`. `partition_files`, `partition_counts` and `partition_bounds` read each partition's rows and bounds from the manifests of any snapshot. `copy_edges` gives a copy's chunk edges. `local_catalog` makes a SQLite catalog in a directory. `register_files` adds files to an Iceberg table. It returns each file's `data_file`: its size and last-modified time. `iceberg_table_attr` pins the table and snapshot. `declare_copy` makes `uint8` arrays whose chunks reference the files. Each reference carries the file's last-modified time. So Icechunk refuses to read a file that changed. A copy has a rectilinear grid. So a reader sets `zarr.config.set({"array.rectilinear_chunks": True})`. |

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
