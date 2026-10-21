# virtual-core and VirtualiZarr

VirtualiZarr turns a file into chunk references (`ManifestArray`,
`ManifestGroup`) and writes them into an Icechunk store. virtual-core covers
what an ingest needs around that: opening remote files to index them, creating
the repository, and a few array layouts VirtualiZarr has no parser for.

Compared against VirtualiZarr 2.8.0 plus
[#1108](https://github.com/zarr-developers/VirtualiZarr/pull/1108)
(`ManifestGroup.to_icechunk`), the version pinned in this workspace.

| virtual-core | What it does | In VirtualiZarr | Headed |
| --- | --- | --- | --- |
| `icechunk_io` | Creates a repo with a virtual chunk container and credentials per source prefix; reopens it authorized to read them; commits with rebase onto concurrent writers | `to_icechunk` writes references into a store you already opened; container and credential setup is the caller's | upstream |
| `builders.flat_binary` | Any uncompressed C-ordered array from shape, dtype, offset and stride, including per-record headers | No general parser; `ENVIParser` (2.8, prototype) covers one flat format | upstream |
| `multiscale` | OME-Zarr pyramid with a referenced level 0 and native levels written by ngff-zarr | None | upstream |
| `fetch` | Fetches a remote file, or only the regions a parser needs, by parallel range requests so it can be indexed locally | None | upstream |
| `remote` | Seekable, caching reader over a remote file for parsers that walk their own index | Overlaps obspec-utils' readers | upstream (obspec-utils) |
| `registry` | Builds an `ObjectStoreRegistry` for a path or URL, with anonymous S3 and the region from the environment | You build the registry yourself | local |
| `inspect` | Asks a remote TIFF where its directories sit and what decoding it needs, before indexing | None | `virtual-tifffile` |
| `progress`, `decisions`, `bench` | Progress lines for long jobs, recorded judgement calls, remote read benchmarks | Out of scope | local |
