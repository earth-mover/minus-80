# virtual-core and VirtualiZarr

virtual-core is the set of wrappers and workarounds our ingests needed on top
of VirtualiZarr to run end to end: repo setup, faster indexing of remote files,
and a few layouts VirtualiZarr has no parser for. Nearly all of it is headed
upstream; only the ingest bookkeeping (progress, decisions, benchmarks) stays.

| virtual-core | What it does | In VirtualiZarr | Upstream |
| --- | --- | --- | --- |
| `icechunk_io` | Creates a repo with a virtual chunk container and credentials per source prefix; reopens it authorized to read them; commits with rebase onto concurrent writers | `to_icechunk` writes references into a store you already opened ([#1108](https://github.com/zarr-developers/VirtualiZarr/pull/1108)); container and credential setup is the caller's | not filed |
| `builders.flat_binary` | Any uncompressed C-ordered array from shape, dtype, offset and stride, including per-record headers | No general parser; `ENVIParser` covers one flat format | not filed |
| `multiscale` | OME-Zarr pyramid with a referenced level 0 and native levels written by ngff-zarr | None | not filed; likely ngff-zarr |
| `fetch` | Fetches a remote file, or only the regions a parser needs, by parallel range requests so it can be indexed locally | None | not filed; possibly obstore |
| `remote` | Seekable, caching reader over a remote file for parsers that walk their own index | Overlaps obspec-utils' readers | not filed; obspec-utils |
| `registry` | Builds an `ObjectStoreRegistry` for a path or URL, with anonymous S3 and the region from the environment | You build the registry yourself | not filed |
| `inspect` | Asks a remote TIFF where its directories sit and what decoding it needs, before indexing | None | moves to `virtual-tifffile` |
| `progress`, `decisions`, `bench` | Progress lines for long jobs, recorded judgement calls, remote read benchmarks | Out of scope | stays local |

## The pinned VirtualiZarr

The workspace pins
[`ianhi/VirtualiZarr@bio-integration`](https://github.com/ianhi/VirtualiZarr/tree/bio-integration):
v2.8.0 plus

- [#1108](https://github.com/zarr-developers/VirtualiZarr/pull/1108), in review: `ManifestStore.to_icechunk` and `ManifestGroup.to_icechunk`, writing without xarray;
- `np.stack`, `np.concatenate`, `np.expand_dims` and `np.full_like` on `ManifestArray` matching numpy, and `with_dimension_names` for naming an added axis, not yet filed.

Concatenating ragged arrays waits on rectilinear chunk grids
([#954](https://github.com/zarr-developers/VirtualiZarr/pull/954), open).
