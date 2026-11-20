"""Small samples of every part of MICrONS minnie, read from the archive for the archive map.

``diagrams.py --archive`` draws one picture per part of the archive. This
module fetches the data behind those pictures, each a few hundred kilobytes
to a few megabytes of range reads or Neuroglancer precomputed chunks, and
caches the arrays in one ``.npz`` file so the figure can be redrawn without
the network. Every sample comes from the running example of the ingest:
scan 4-7, field 4, ROI 23, and the EM cell CAVE matches it to.

The reads:

- the two-photon frames: 16 pages of field 4 from BossDB's v2 TIFF, one range
  each, at offsets computed from the file's own layout (LAYOUTS.md);
- the NWB file: field 4's ROI masks, ROI 23's trace and one stimulus frame,
  through h5py over a block reader;
- the EM, the segmentation and microCT: Neuroglancer precomputed volumes,
  read with TensorStore, which decodes their shards, JPEG and
  compressed_segmentation chunks;
- the structural stack: one page, through tifffile over a block reader;
- the digital twin: one unit's row of ``responses.npy``, one range after the
  128-byte header.
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np

BOSSDB = "https://bossdb-open-data.s3.amazonaws.com/iarpa_microns/minnie/"
BOSSDB_S3 = "s3://bossdb-open-data/iarpa_microns/minnie/"
GOOGLE = "https://storage.googleapis.com/iarpa_microns/minnie/minnie65/"
TIFF_V2 = "functional_data/two_photon_functional_scans/functional_scan_17797_4_7_v2.tif"
STACK = "functional_data/two_photon_structural_stacks/two_photon_stack_17797_9_19_v1.tiff"
RESPONSES = "functional_data/digital_twin_properties/v2/responses/responses.npy"
SEG_MASKS = "functional_data/field_seg_masks/session_4_scan_7_field_4/"

FIELD, ROI, FIELDS = 4, 23, 8
#: v2 pages start at byte 406 and are 440 x 248 int16 each (LAYOUTS.md, "Each TIFF").
FIRST_PAGE, PAGE_BYTES, PAGE_SHAPE = 406, 440 * 248 * 2, (440, 248)
#: The EM point the NWB file and coregistration_manual_v4 give for ROI 23 of field 4, in 4 x 4 x 40 nm voxels.
EM_POINT = (359_328, 122_528, 17_658)


def two_photon_frames(volume: int = 20_000, count: int = 16) -> np.ndarray:
    """``count`` consecutive volumes of field 4, as the v2 TIFF stores them: (count, rows, columns) int16."""
    from minus80.core.fetch import read_range

    frames = []
    for v in range(volume, volume + count):
        start = FIRST_PAGE + (v * FIELDS + FIELD - 1) * PAGE_BYTES
        frames.append(np.frombuffer(read_range(BOSSDB_S3 + TIFF_V2, start, start + PAGE_BYTES), "<i2"))
    return np.stack(frames).reshape(count, *PAGE_SHAPE)


def nwb_samples(nwb_url: str) -> dict[str, np.ndarray]:
    """Field 4's masks (largest weight per pixel), ROI 23's mask and trace, and one stimulus frame."""
    import h5py

    from minus80.core import remote

    with h5py.File(remote.reader(nwb_url, block=1 << 20, cached_blocks=64), "r") as f:
        masks = f[f"processing/ophys/ImageSegmentation/PlaneSegmentation{FIELD}/image_mask"][()]
        series = f[f"processing/ophys/Fluorescence/RoiResponseSeries{FIELD}"]
        trace, times = series["data"][:, ROI - 1], series["timestamps"][()]
        video = next(k for k in f["acquisition"] if k.startswith("Video"))
        frame = f[f"acquisition/{video}/data"][200_000]
        eye = f["acquisition/EyeTracking/eye_position/data"][:20_000]
        treadmill = f["acquisition/treadmill_velocity/data"][:60_000]
    return {"masks_max": masks.max(axis=0).T, "roi_mask": masks[ROI - 1].T, "trace": trace,
            "trace_times": times, "stimulus_frame": frame, "eye": eye, "treadmill": treadmill}


def precomputed(url: str, *, scale: int, center: tuple[int, ...], size: tuple[int, ...]) -> np.ndarray:
    """A box of ``size`` voxels around ``center`` (in the scale's own voxels), z-section ``center[2]``."""
    import tensorstore as ts

    volume = ts.open({"driver": "neuroglancer_precomputed", "kvstore": url, "scale_index": scale}).result()
    box = tuple(slice(c - s // 2, c - s // 2 + s) for c, s in zip(center, size))
    return np.asarray(volume[box + (0,)].read().result())


def em_samples() -> dict[str, np.ndarray]:
    """EM around ROI 23's matched cell at 8, 32 and 128 nm, the v1300 segmentation at 64 nm, and field 4's label copy."""
    import tensorstore as ts

    out = {}
    x4, y4, z = EM_POINT
    for name, scale, factor in (("em_8nm", 0, 2), ("em_32nm", 2, 8), ("em_128nm", 4, 32)):
        # Sections are 40 nm down to the 64 nm scale; the 128 nm scale halves them to 80 nm.
        section = z if scale < 4 else z // 2
        out[name] = precomputed(BOSSDB + "minnie65/em/", scale=scale,
                                center=(x4 // factor, y4 // factor, section), size=(256, 256, 1))[..., 0]
    # The 64 nm scale halves the sections to 80 nm, like the EM's 128 nm scale.
    out["seg_64nm"] = precomputed(GOOGLE + "seg_m1300/", scale=3, center=(x4 // 16, y4 // 16, z // 2),
                                  size=(256, 256, 1))[..., 0]
    masks = ts.open({"driver": "neuroglancer_precomputed", "kvstore": BOSSDB + SEG_MASKS}).result()
    out["field_seg_mask"] = np.asarray(masks[..., 0, 0].read().result())
    return out


def microct_slice() -> np.ndarray:
    """The middle slice of the microCT volume along its third axis, at its 3.76 micrometer scale."""
    import tensorstore as ts

    volume = ts.open({"driver": "neuroglancer_precomputed", "kvstore": BOSSDB + "microCT/17797_1R_0.94um/",
                      "scale_index": 2}).result()
    middle = (volume.domain[2].inclusive_min + volume.domain[2].exclusive_max) // 2
    return np.asarray(volume[:, :, middle, 0].read().result())


def stack_slice(page: int = 335) -> np.ndarray:
    """One page of the two-photon structural stack, float32."""
    import tifffile

    from minus80.core import remote

    with tifffile.TiffFile(remote.reader(BOSSDB_S3 + STACK, block=1 << 20, cached_blocks=16)) as tiff:
        return tiff.pages[page].asarray()


def digital_twin_row(unit: int = 0, frames: int = 3_000) -> np.ndarray:
    """The first ``frames`` model responses of one unit, from the v2 ``responses.npy`` (units x 75,000 float32)."""
    from minus80.core.fetch import read_range

    header = read_range(BOSSDB_S3 + RESPONSES, 0, 128)
    if b"(104171, 75000)" not in header or b"<f4" not in header:
        raise ValueError(f"{RESPONSES}: unexpected header {header!r}")
    start = 128 + unit * 75_000 * 4
    return np.frombuffer(read_range(BOSSDB_S3 + RESPONSES, start, start + frames * 4), "<f4")


def fetch_all(nwb_url: str) -> dict[str, np.ndarray]:
    samples = {"two_photon": two_photon_frames()}
    samples |= nwb_samples(nwb_url)
    samples |= em_samples()
    samples["microct"] = microct_slice()
    samples["stack"] = stack_slice()
    samples["digital_twin"] = digital_twin_row()
    return samples


def cached(path: Path, nwb_url: str) -> dict[str, np.ndarray]:
    """The samples from ``path``, fetching and saving them first if it does not exist."""
    if not path.exists():
        samples = fetch_all(nwb_url)
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **samples)
        path.write_bytes(buffer.getvalue())
    with np.load(path) as data:
        return {k: data[k] for k in data.files}
