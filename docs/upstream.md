# Upstream

Each row is code in minus80 that exists because a library it depends on lacks
something, and the upstream change that would let minus80 drop it.

Statuses checked 2026-10-22.

## VirtualiZarr

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| The workspace pins a VirtualiZarr fork, v2.8.0 plus extra commits, so a `ManifestGroup` can be written to Icechunk without building an xarray Dataset. | root `pyproject.toml` (`[tool.uv.sources]`), `minus80.core.icechunk_io` | Merge and release `ManifestStore.to_icechunk` and `ManifestGroup.to_icechunk`, then replace the pin with the release. | [VirtualiZarr#1108](https://github.com/zarr-developers/VirtualiZarr/pull/1108), open PR |
| The same fork makes `np.stack` and `np.expand_dims` on a `ManifestArray` keep `dimension_names` the same length as the shape, and adds `with_dimension_names` for naming an axis. | root `pyproject.toml`, `minus80.parsers.nwb` | Contribute these `ManifestArray` changes. | not filed |
| VirtualiZarr's `np.stack` and `np.concatenate` take every attribute from the first array, so `source_url` names one of the files the merged manifest references, and attributes that differ between the inputs, such as pixel sizes, go unchecked. `stack` and `concatenate` set `source_url` from the merged manifest and raise when the other attributes differ. | `minus80.core.manifest` | Merge or check attributes when combining `ManifestArray`s. | not filed |
| `ManifestArray` has no `permute_dims` (a TODO in `manifests/array_api.py`). `permute_dims` permutes the chunk grid and adds Zarr's `transpose` codec when the order inside a chunk changes. | `minus80.core.manifest` | Implement `permute_dims` by reordering chunks and adding a transpose codec. | [VirtualiZarr#872](https://github.com/zarr-developers/VirtualiZarr/issues/872) and [#163](https://github.com/zarr-developers/VirtualiZarr/issues/163), open issues |
| `HDFParser` names every axis that has no HDF5 dimension scale `phony_dim_<n>`. The NWB parser replaces those names with null, because the file states no name. | `minus80.parsers.nwb` | Leave unscaled axes unnamed in `HDFParser`, and supply placeholder names only when converting to xarray, which needs them. | not filed |
| The NWB parser classifies every dataset with h5py and passes the variable-length strings, compound datasets, object references and scalars to `HDFParser` as `drop_variables`, then carries them as JSON attributes. Without that, `HDFParser` raised on them. | `minus80.parsers.nwb` | Read these datasets in `HDFParser`. | Released in v2.8.0, which the pin includes: strings ([#1121](https://github.com/zarr-developers/VirtualiZarr/pull/1121)), compound datasets ([#1126](https://github.com/zarr-developers/VirtualiZarr/pull/1126)) and opt-in object references ([#1125](https://github.com/zarr-developers/VirtualiZarr/pull/1125)). The NWB parser has not been moved onto them. The tracking issue, [#1104](https://github.com/zarr-developers/VirtualiZarr/issues/1104), is still open, as is [#1137](https://github.com/zarr-developers/VirtualiZarr/pull/1137), which fixes the chunk grid of datasets with missing chunks and of inlined strings. |
| A FLAC collection needs a chunk edge at every file boundary, and `ManifestArray` accepts only regular chunk grids, so the FLAC parser writes its references with Icechunk's `set_virtual_refs` directly. | `minus80.parsers.flac` | Support rectilinear chunk grids in `ManifestArray`. | [VirtualiZarr#954](https://github.com/zarr-developers/VirtualiZarr/pull/954), open PR |
| `create_repo` adds a virtual chunk container and a credential for each source prefix, and `open_repo` reads the prefixes back from the repo's config to authorize them. `to_icechunk` leaves all of this to the caller. | `minus80.core.icechunk_io` | Derive the containers and credentials from the source URLs a manifest already carries. | not filed |
| `registry_for` builds an `ObjectStoreRegistry` for a path or URL, with anonymous S3 access and the bucket's region, looked up from S3 when the environment does not set one. VirtualiZarr has the caller build the registry by hand. | `minus80.core.registry` | Build a registry from a URL. | not filed |
| obstore and icechunk address an S3 bucket in us-east-1 unless told its region, and a bucket elsewhere answers with a redirect neither follows. `s3_region` asks S3 for the bucket's region with an unsigned `HEAD` and passes it to both. object_store has `resolve_bucket_region` for this; obstore does not expose it. | `minus80.core.registry` | obstore: expose `resolve_bucket_region`, or resolve the region in `from_url` when none is configured. | not filed |
| VirtualiZarr has no parser for an uncompressed, C-ordered array at a known offset, so `flat_binary` builds one from shape, dtype, offset and stride. The stride covers formats that write a header before each record. | `minus80.core.builders.flat_binary` | Add a general flat-binary parser with a stride parameter. `ENVIParser` covers one such format. | not filed |
| The zip parser makes each member of a zip one chunk of an array, for archives of TIFFs or other non-Zarr files. `ZippedZarrParser` reads only zipped Zarr stores, and its docstring lists wrapping other formats as future work. | `minus80.parsers.zip.members` | Add a member-per-chunk helper beside `ZippedZarrParser`. | not filed |
| `parse_zip_index` fetches the whole central directory in one request, which is 1.49 GB for an archive of 7.7 million members. `buffers` walks the local headers in order instead. | `minus80.parsers.zip.buffers` | Read the central directory in bounded pieces. | not filed |
| `buffers` imports VirtualiZarr's private `_parse_zip64_extra` to read the sizes in a ZIP64 local header. | `minus80.parsers.zip.buffers` | Make it public. | not filed |
| `tensor_ranges` reads a safetensors header and returns each tensor's byte range, including inside a STORED zip member. | `minus80.parsers.zip.safetensors_ranges` | Add a safetensors parser. | [VirtualiZarr#367](https://github.com/zarr-developers/VirtualiZarr/issues/367), open issue |
| `ZippedZarrParser` reads a `.zarr.zip` that is its own object. It cannot read one stored inside another zip. `member_file` opens the stored member as a seekable file, and `copy_group` copies the Zarr v2 chunks into a v3 group. | `minus80.parsers.zip.nested`, `minus80.core.zarr_copy` | Let `ZippedZarrParser` read a Zarr zip that is a member of a stored zip. | not filed |

## Icechunk

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| A repo stored locally gives its virtual chunk reads the local disk's storage settings: a 4 KiB request size, which splits a remote read into many small GETs, and a single try, so one dropped connection on any of an array's GETs fails the read ("connection closed before message completed"). `create_repo` stores a 12 MiB request size and 10 tries whenever a source is remote, and `open_repository` reads with 10 tries even when the stored config lacks them. | `minus80.core.icechunk_io` | Stop virtual chunk storage from inheriting the repo's local storage settings. | [icechunk#2256](https://github.com/earth-mover/icechunk/issues/2256), open issue, names only the request size; [icechunk#1431](https://github.com/earth-mover/icechunk/issues/1431), open issue, reports the error without the cause |
| Icechunk fetches manifests one at a time while committing. At 50 ms latency, a commit of 200 arrays took 11.45 s that way and 0.83 s with concurrent fetches, so `icechunk_io` sets the commit's manifest fetch concurrency to 32. | `minus80.core.icechunk_io` | Raise the default for `max_concurrent_manifest_fetches_during_commit`. An Arraylake repo's stored config keeps the default too, so `arraylake_repository` sets it for the session. | [icechunk#2273](https://github.com/earth-mover/icechunk/issues/2273), closed by [#2274](https://github.com/earth-mover/icechunk/pull/2274), which exposed the knob and kept the default of 1; raising the default is not filed |
| Icechunk drops the query string from a virtual chunk's URL when it reads the chunk, so the request for a chunk behind a signed or tokened link goes out without its token. `store_and_key` keeps the query when indexing such a source; reading the chunks back waits on this fix. | `minus80.core.registry` | Keep the query string on read for HTTP(S) virtual chunks. | [icechunk#2444](https://github.com/earth-mover/icechunk/issues/2444), open issue |

## virtual-tiff and async-tiff

The TIFF parser exists because virtual-tiff cannot yet open most whole-slide
and microscopy TIFFs. Once these rows are fixed, `minus80.parsers.tiff` can be
retired in favour of virtual-tiff.

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| virtual-tiff refuses JPEG tiles that share a `JPEGTables` tag ([`parser.py#L203`](https://github.com/virtual-zarr/virtual-tiff/blob/9d952019cc59432af86de827116365d03f0dddcf/src/virtual_tiff/parser.py#L203)), which slide scanners commonly write. The TIFF parser builds its manifests from tifffile and puts the tables in the codec configuration. | `minus80.parsers.tiff` | virtual-tiff: carry the tables and the colour spaces in the JPEG codec's configuration. | not filed |
| virtual-tiff refuses YCbCr photometric interpretation ([`parser.py#L387`](https://github.com/virtual-zarr/virtual-tiff/blob/9d952019cc59432af86de827116365d03f0dddcf/src/virtual_tiff/parser.py#L387)). The TIFF parser takes the colour spaces from tifffile's `jpeg_decode_colorspace`, and libjpeg converts to RGB during the decode. | `minus80.parsers.tiff` | virtual-tiff: accept YCbCr for JPEG compression. | not filed |
| virtual-tiff wraps numcodecs v2 codecs as Zarr 3 bytes-to-bytes codecs, so its stores do not open with `imagecodecs-zarr` alone. The TIFF parser writes imagecodecs-zarr's own codecs. | `minus80.parsers.tiff` | virtual-tiff: write imagecodecs-zarr's codec chains and drop its own codecs. | [virtual-tiff#109](https://github.com/virtual-zarr/virtual-tiff/issues/109), open issue |
| virtual-tiff turns each image file directory into its own array, so resolution levels are not grouped into a pyramid. The TIFF parser uses tifffile's `series`, which does that grouping for vendor formats. | `minus80.parsers.tiff` | virtual-tiff: group directories into pyramid levels. | not filed |
| async-tiff's metadata cache holds one contiguous block from byte 0 ([`cache.rs#L99`](https://github.com/developmentseed/async-tiff/blob/7dd6c80062c2f8750bf8b581c6860bce4e37fd94/src/metadata/cache.rs#L99)), so reaching a directory near the end of a file fetches nearly every byte before it. The TIFF parser reads directories through tifffile instead. | `minus80.parsers.tiff` | async-tiff: cache sparse byte ranges. | [async-tiff#344](https://github.com/developmentseed/async-tiff/issues/344), open issue |
| async-tiff prefetches a prefix of the file sized for a Cloud-Optimized GeoTIFF, which prefetches almost nothing when the directories sit at the end of the file, as bioformats2raw writes them. `directory_layout` detects a contiguous block of equal-sized directories from four reads, and `sparse_local` can then fetch that block alone into a local file. | `minus80.parsers.tiff.inspect`, `minus80.core.fetch` | async-tiff: prefetch the directory region rather than a prefix. | not filed |

## imagecodecs

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| A Deflate zip member is a bare Deflate stream. imagecodecs-zarr's `Deflate(raw=True)` cannot decode one without being told its uncompressed size, because `deflate_decode` with `raw=True` needs `out`. The zip parser therefore accepts only STORED members as chunks. | `minus80.parsers.zip.members` | Decode raw Deflate when the output size is unknown. | not filed |

## ngff-zarr

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| `multiscale` writes an OME-Zarr pyramid whose level 0 is virtual references and whose lower levels are native chunks written by ngff-zarr. | `minus80.core.multiscale` | Let the pyramid writer accept a virtual base level. | not filed |

## obstore and obspec-utils

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| A parser walking a remote file's index makes thousands of small, partly backward reads. `remote` opens the file with a 64 KB block and a 256-block cache, through a store `registry` builds with a 300 s timeout and a retry backoff from 2 s to 60 s over 20 minutes. Walking the index of a 1,024-chunk HDF5 file was forty times slower with obspec-utils' default 1 MB read-ahead. | `minus80.core.remote`, `minus80.core.registry` | obspec-utils: add a reader configured for indexing walks. | not filed |
| tifffile and h5py read by seeking a file, so they cannot be handed a list of byte ranges to fetch. `download_then_index` fetches a remote file with parallel range requests, and `sparse_local` writes just the regions a parser needs into a sparse local file. | `minus80.core.fetch` | obstore, possibly: add a parallel range-download helper. | not filed |
| `iter_ranges` issues one `get_range` per block on a thread pool, because `get_ranges` merges adjacent ranges into one request even with `coalesce=0`. | `minus80.core.fetch` | obstore / object_store: make `coalesce=0` keep adjacent ranges separate, or document that it does not. | not filed |
| `ObjectStoreRegistry.resolve` strips the store's decoded prefix from the URL's still-encoded path, so a local file under a directory with a space or `+` gets its whole path as the key; and it returns the key undecoded, so `a+b.bin` is looked up as `a%2Bb.bin`. `store_and_key` resolves local URLs without it, and `resolve`, which reads through a caller's registry, takes a local key from the decoded path. `registry_for` still has the bug, so VirtualiZarr reads and `HDFParser` indexing of such paths fail. | `minus80.core.registry` | obspec-utils: decode the URL path before stripping the store prefix. | not filed |

## flac-numcodecs

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| The `flac` Zarr 3 codec exists only on the `zarr3-flac-codec-2026-10-19` tag of a flac-numcodecs fork, which the root `pyproject.toml` pins in `[tool.uv.sources]`. | `minus80.parsers.flac` | Merge the codec into flac-numcodecs and release it. | not filed |

## zarr

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| `submit` starts a coroutine on zarr's IO loop through the private `zarr.core.sync._get_loop`, so range GETs can be cancelled and the loop's daemon thread does not hold the interpreter open. `zarr.core.sync.sync` runs a coroutine to completion but returns no handle to cancel it. | `minus80.core.aio` | Expose a public way to submit a coroutine to zarr's IO loop and get a cancellable future. | no issue found |
| A regular chunk grid cannot hold a virtual reference to a file's short last chunk. `declare_copy` writes a rectilinear grid, which needs the `array.rectilinear_chunks` config flag to open. | `minus80.core.tables` | Enable rectilinear chunk grids without a config flag. | not filed |

## SpatialData

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| SpatialData cannot open a store that has no filesystem path. The ingest keeps each points and shapes Parquet file as a `uint8` array, which `declare_copy` references from the repo. | `minus80.core.tables` | Open points and shapes from a store without a filesystem path. | [spatialdata#1107](https://github.com/scverse/spatialdata/pull/1107), open PR |

## pyiceberg

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| pyiceberg maps Arrow `uint32` to Iceberg's signed `int`, which overflows on 4294967295. The ingest declares such columns as `int64`. | `ingests/atera/transcripts.py` | Map unsigned integers to a type that holds their range. | not filed |
| pyiceberg `add_files` leaves files without field ids. It registers each file as written and sets the table's `schema.name-mapping.default`. `write_parquet` gives each column the field id of the table schema, and pyiceberg 0.12.0 `add_files` accepts such a file. The field ids stay the chosen form. | `minus80.core.tables` | zax: read a file without field ids through the table's name mapping, as the Iceberg spec allows. | Done in zax on 2026-11-11: zax reads name-mapped files (`ingests/atera/LAYOUTS.md`, check (d)) |

## Python standard library

| Workaround | Module | Upstream | Status |
| --- | --- | --- | --- |
| `buffers` calls the private `zipfile._EndRecData` to read an end-of-central-directory record, ZIP64 included, from an archive's tail. It passes the archive's size because recent Python patch releases check the ZIP64 locator's absolute offset. | `minus80.parsers.zip.buffers` | Add a public reader for the end record. | not filed |
