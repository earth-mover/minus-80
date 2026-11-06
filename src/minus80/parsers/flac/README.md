# minus80.parsers.flac

FLAC audio files as Icechunk virtual references, decoded by a `flac` Zarr v3 codec.
One file or many become one array on a single time axis, and the audio stays in
the source files.

```bash
uv sync --extra flac
```

```python
from minus80.parsers.flac import chunk_table, scan_frames, write_array

scans = [scan_frames(source) for source in sources]   # paths or URLs
table = chunk_table(scans)                             # one frame per chunk
write_array(group, "waveform", table)                  # group in a writable Icechunk session
```

| Function | What it does |
| --- | --- |
| `scan_frames(source, url=None)` | Byte offset, byte length and sample count of every frame in one file, read with libavformat (PyAV). Returns a `FrameScan`. |
| `chunk_table(scans, frames_per_chunk=1)` | Groups each file's frames into chunks and checks that one codec configuration decodes them all. Returns a `ChunkTable`. |
| `ChunkTable.save` / `.load` | Round-trip a table through `.npz`, so an array can be rebuilt without reading audio again |
| `write_array(group, name, table, dimension_names=None, attributes=None)` | Creates the array and sets its virtual references in the group's Icechunk store. `attributes` are made JSON-safe, so NaN is stored as `null`. |

`source` is one of:

- a local path or `file://` URL, which PyAV opens itself;
- a remote URL (`http(s)://`, `s3://`, or any scheme `minus80.core.registry`
  resolves), read through `minus80.core.remote.reader` in 8 MiB blocks;
- an open binary file, which needs `url=`, the address the references record.
  Without it, `scan_frames` raises `ValueError`.

Pass `url=` with a path or URL too when the references should point somewhere
else, such as a local copy of a remote file.

## What it buys

| From | What |
| --- | --- |
| PyAV (libavformat) | Frame offsets, sizes and durations, sample rate, channel count, sample format |
| [flac-numcodecs](https://github.com/ianhi/flac-numcodecs/tree/zarr3-flac-codec-2026-10-19), tag `zarr3-flac-codec-2026-10-19` | The `flac` Zarr v3 codec. Installing it registers the codec with Zarr. |
| Icechunk | `set_virtual_refs`, which stores the references |

## How FLAC maps onto Zarr

A FLAC file holds a short metadata section followed by a run of frames. Each frame
is byte-aligned and decodes on its own. Its header states the frame's own sample
rate, bit depth and channel layout, so a run of consecutive frames decodes without
the file header. A chunk is therefore a run of whole frames, and one codec
configuration (`blocksize`, `sample_rate`) serves every file that shares them.

- **One frame per chunk by default.** That matches the file's own storage unit.
  At 4,096 samples a frame it comes to tens of thousands of references per hour
  of audio; pass `frames_per_chunk` to merge frames, and record why.
- **The grid is rectilinear.** Each file's last chunk ends at its last sample, and
  no chunk spans two files, so chunk lengths differ at file boundaries. Readers
  need `zarr.config.set({"array.rectilinear_chunks": True})`.
- **Arrays are always `("time", "channel")`.** Mono has a length-1 channel axis.
  All channels sit in one chunk, because FLAC stores every channel of a frame
  together.
- **Every frame is located by reading the whole file.** A SEEKTABLE could locate
  seekpoints from the first megabyte, but the block is optional: many encoders
  omit it, and where present it indexes only every few hundred frames, at
  irregular spacing.

## Icechunk directly, not through VirtualiZarr

The other parsers build VirtualiZarr manifests and leave writing to
`minus80.core.icechunk_io`. This one writes into Icechunk itself, because the
grid it needs is rectilinear and `ManifestArray` accepts only regular chunk
grids ([VirtualiZarr#954](https://github.com/zarr-developers/VirtualiZarr/pull/954)).

`write_array` calls Icechunk's `set_virtual_refs`, which skips a reference that
matches no virtual chunk container and returns its index instead of raising.
`write_array` raises in that case, and when a chunk has no bytes, rather than
leave an array that reads as fill.

When VirtualiZarr accepts rectilinear grids, `write_array` can give way to a
function that returns a `ManifestArray`.

## Limits

- **16-bit only.** The codec decodes 16-bit samples, so `chunk_table` refuses
  other bit depths.
- **Fixed blocksize.** Every frame except a file's last must hold the same number
  of samples. `chunk_table` refuses a variable-blocksize stream.
- **Files in one array must agree** on sample rate, blocksize and channel count.
  Files that differ go in separate arrays.
- **The codec is unreleased.** It lives on a tag of flac-numcodecs, which the
  root `pyproject.toml` pins in `[tool.uv.sources]`.
- **Synthetic test files are regular.** libsndfile writes FLAC with no SEEKTABLE
  and a uniform frame grid. The tests cover the decode path; check a new archive
  against one of its own files, decoded by libsndfile.
