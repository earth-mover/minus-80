"""A small scan with the structure of a MICrONS two-photon scan, for testing the ingest locally.

What the ingest relies on is reproduced at a smaller size:

- two TIFF exports of one movie of shape (volumes * fields, rows, columns),
  written by tifffile as the archive's were: one shaped series, the first
  directory at byte 16, every page one uncompressed strip, back to back, and
  the other directories packed after the pixels;
- the pages interleaved field by field, page ``v * FIELDS + n - 1`` being
  field ``n`` of volume ``v``;
- export 1 uint16 with resolution tags of unit NONE, export 2 int16 without
  them, over the same bytes, so negative pixels read as large ones in
  export 1;
- an NWB-shaped HDF5 file with one ``TwoPhotonSeries<n>`` per field, its data
  the field's frames transposed, every series carrying the same timestamps,
  the rate only in the description, and an imaging plane per field holding
  its center and depth in ``origin_coords`` and its pixel size in
  ``grid_spacing``, in meters;
- per field, a ``PlaneSegmentation<n>`` of ``ROIS[n - 1]`` overlapping ROI
  weight masks, transposed like the movie and ids 1 to n, and a
  ``RoiResponseSeries<n>`` of traces, as the DANDI files lay out the
  processed data.

    uv run --package microns-ingest python ingests/microns/fixture.py <dir>
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np
import tifffile

FIELDS, VOLUMES, ROWS, COLUMNS = 4, 30, 12, 8
RATE_HZ = 6.3009
PIXEL_M = 2.5e-6
#: Two depths with two coplanar fields each, as the real scans pair them.
CENTERS_M = [(-4.85e-4, -2.35e-4, 8e-5), (9.5e-5, -2.35e-4, 8e-5),
             (-4.85e-4, -2.35e-4, 2.2e-4), (9.5e-5, -2.35e-4, 2.2e-4)]
ANIMAL, SESSION, SCAN = 17797, 4, 7
#: ROIs per field; their sum is the scan's unit count.
ROIS = (3, 5, 2, 4)


@dataclass(frozen=True)
class Truth:
    pages: np.ndarray
    masks: dict[int, np.ndarray]
    times: np.ndarray
    tiffs: dict[int, Path]
    nwb: Path


def build(directory: Path) -> Truth:
    """Write both TIFF exports and the NWB-shaped file into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    pages = rng.integers(-200, 4000, size=(VOLUMES * FIELDS, ROWS, COLUMNS), dtype=np.int16)
    times = 10.0 + np.arange(VOLUMES) / RATE_HZ + rng.normal(0, 1e-4, VOLUMES)
    stem = f"functional_scan_{ANIMAL}_{SESSION}_{SCAN}"
    tiffs = {1: directory / f"{stem}_v1.tif", 2: directory / f"{stem}_v2.tif"}
    tifffile.imwrite(tiffs[1], pages.view(np.uint16), bigtiff=True, photometric="minisblack",
                     resolution=(1, 1), resolutionunit="NONE")
    tifffile.imwrite(tiffs[2], pages, bigtiff=True, photometric="minisblack")
    nwb = directory / f"sub-{ANIMAL}_ses-{SESSION}-scan-{SCAN}_behavior+image+ophys.nwb"
    masks = {n: roi_masks(rng, ROIS[n - 1]) for n in range(1, FIELDS + 1)}
    write_nwb(nwb, pages, times, masks)
    return Truth(pages=pages, masks=masks, times=times, tiffs=tiffs, nwb=nwb)


def roi_masks(rng, count: int) -> np.ndarray:
    """``count`` weight masks, (roi, rows, columns): overlapping blobs of positive weights."""
    masks = np.zeros((count, ROWS, COLUMNS), dtype=np.float32)
    for r in range(count):
        top, left = rng.integers(0, ROWS - 4), rng.integers(0, COLUMNS - 4)
        masks[r, top:top + 5, left:left + 5] = rng.uniform(0.01, 3, (5, 5))
    return masks


def write_nwb(path: Path, pages: np.ndarray, times: np.ndarray, masks: dict[int, np.ndarray]) -> None:
    """The parts of a DANDI 000402 NWB file the ingest reads, laid out as those files lay them out."""
    with h5py.File(path, "w") as f:
        f.attrs["neurodata_type"] = "NWBFile"
        f.attrs["namespace"] = "core"
        device = f.create_group("general/devices/Mesoscope")
        device.attrs["description"] = "two-photon random access mesoscope"
        for n in range(1, FIELDS + 1):
            x, y, depth = CENTERS_M[n - 1]
            plane = f.create_group(f"general/optophysiology/ImagingPlane{n}")
            plane["description"] = f"The imaging plane for field {n} at {depth} meters depth.".encode()
            plane["grid_spacing"] = np.array([PIXEL_M, PIXEL_M])
            plane["grid_spacing"].attrs["unit"] = "meters"
            plane["origin_coords"] = np.array([x, y, depth])
            plane["origin_coords"].attrs["unit"] = "meters"
            plane["imaging_rate"] = np.nan
            plane["indicator"] = b"GCaMP6"
            plane["location"] = b"VISp,VISrl,VISlm,VISal"
            plane["excitation_lambda"] = 920.0
            plane["OpticalChannel/emission_lambda"] = 500.0
            plane["device"] = h5py.SoftLink("/general/devices/Mesoscope")
            series = f.create_group(f"acquisition/TwoPhotonSeries{n}")
            series.attrs["description"] = (f"Calcium imaging data for field {n} at {RATE_HZ} Hz "
                                           f"and {depth} meters depth.")
            series.create_dataset("data", data=pages[n - 1::FIELDS].transpose(0, 2, 1),
                                  chunks=(VOLUMES, 4, 4), compression="gzip")
            series["timestamps"] = times
            series["field_of_view"] = np.array([COLUMNS * PIXEL_M, ROWS * PIXEL_M])
            series["dimension"] = np.array([ROWS, COLUMNS])
            series["imaging_plane"] = h5py.SoftLink(f"/general/optophysiology/ImagingPlane{n}")
            table = f.create_group(f"processing/ophys/ImageSegmentation/PlaneSegmentation{n}")
            table.attrs["neurodata_type"] = "PlaneSegmentation"
            table.create_dataset("image_mask", data=masks[n].transpose(0, 2, 1), chunks=(2, 4, 4),
                                 compression="gzip")
            table["id"] = np.arange(1, len(masks[n]) + 1)
            traces = f.create_group(f"processing/ophys/Fluorescence/RoiResponseSeries{n}")
            traces.attrs["neurodata_type"] = "RoiResponseSeries"
            traces.create_dataset("data", data=np.arange(VOLUMES * len(masks[n]), dtype=np.float32)
                                  .reshape(VOLUMES, len(masks[n])), chunks=(10, 2), compression="gzip")
            traces["timestamps"] = times
            traces["rois"] = np.arange(len(masks[n]))


if __name__ == "__main__":
    truth = build(Path(sys.argv[1]))
    print(f"wrote {', '.join(p.name for p in (*truth.tiffs.values(), truth.nwb))} to {sys.argv[1]}")
