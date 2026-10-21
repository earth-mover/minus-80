# virtual-core

The small, shared layer under the bio-virtualization subprojects. Deliberately thin:
it holds only what VirtualiZarr does **not** already give you for bio formats, and
those pieces are staged for upstream contribution (see [../../UPSTREAM.md](../../UPSTREAM.md)).

For HDF5/NetCDF/TIFF/Zarr/GRIB, use VirtualiZarr's own parsers or the
`icechunk-datacube-ingestion` skill directly — **not** this.

## What's here

| Module | Purpose | Status |
|---|---|---|
| `builders/flat_binary` | Virtualize a flat/headerless uncompressed array from (shape, dtype, offset) — the gap VirtualiZarr has no parser for. | Real, validated. → upstream `FlatBinaryParser`. |
| `registry` (`as_url`, `registry_for`) | Object-store registry glue (local + cloud). | Real (deduped). |
| `icechunk_io` | `create_repo` (virtual-chunk containers per source prefix), `write_manifest_group` (writes with VirtualiZarr's `ManifestGroup.to_icechunk`, no xarray), and self-describing reopen. | Real. Container wiring → upstream candidate. |
| `multiscale` | Hybrid OME-Zarr multiscale: virtual base + native ngff-zarr pyramid in one repo (`imaging` extra). | Real (promoted from virtual-cryoet). → upstream ngff-zarr × virtualizarr. |
| `fetch` (`download_then_index`) | Pull a remote file once via parallel range GETs for local metadata walking. | Real (deduped). → maybe upstream. |
| `bench` | Shared benchmark protocol (`BenchTarget`). | **Interface only — extract the runner from `virtual/benchmarks/bench.py`.** |

## Validated

`flat_binary` → `ManifestGroup` → read back through a real
`ManifestStore` gives byte-identical roundtrips and correct slicing.

## Install

```bash
uv sync            # add --group bench for snailmail
```

## Reconciliation (surfaced by virtual-cryoet + virtual-nwb)

Done:
- [x] `open_repo` self-authorizes from repo config (both subprojects delegate to it).
- [x] `write_multiscale` promoted to `virtual_core.multiscale` (cryoet is a thin MRC
  adapter over it); `create_repo` factored out of `icechunk_io` and shared.
- [x] Deprecated `authorize_virtual_chunk_access={prefix: None}` replaced with the
  `LocalFileSystemAccess` sentinel (in `create_repo` + `open_repo`); warning gone.

Deferred (deliberately — not worth destabilizing the green suites now):
- [ ] `write_manifest_group(…, native=…)`: write virtual arrays + real native zarr
  arrays in one repo. NWB's root-attrs approach works today; add when a subproject
  needs large native tables as arrays rather than JSON attrs.
- [ ] `registry`/`as_url`: reserved chars in filenames (`+` → `%2B`). Edge case;
  subprojects rename local copies. Fix with a targeted test later.
