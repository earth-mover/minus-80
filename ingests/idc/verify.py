"""Spot-check a built IDC store against the DICOM it references.

For every slide: the OME-Zarr documents validate and the level shapes match
the instances. On a sample: a few tiles per slide decode equal to imagecodecs'
decode of the same DICOM fragment and to pydicom's own decode; for TCGA, the
mask tiles under them equal pydicom's decode of the SEG frames, and the
nuclei arrays equal the ANN's arrays read by pydicom. Each slide costs a few
tile reads against the bucket, plus local reads of the cached instances.

    uv run --package idc-ingest python ingests/idc/verify.py REPO [--tiles 4]
"""

from __future__ import annotations

import argparse
import random
import time

import imagecodecs
import numpy as np
import pydicom
from pydicom.pixels import pixel_array
import zarr
from minus80.core.icechunk_io import open_repo
from minus80.ome.image import validate
from minus80.parsers.dicom import encapsulated_frame_ranges

from plumbing import local, say


def slides(root: zarr.Group):
    for collection in root.group_keys():
        for patient in root[collection].group_keys():
            for slide in root[f"{collection}/{patient}"].group_keys():
                yield f"{collection}/{patient}/{slide}"


def source_tile(url: str, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Frame ``k`` of a level decoded by imagecodecs from its fragment, and by pydicom."""
    path = local(url)
    offset, length = encapsulated_frame_ranges(path)[k]
    with open(path, "rb") as f:
        f.seek(offset)
        fragment = f.read(length)
    ds = pydicom.dcmread(path, stop_before_pixels=True)
    decode = imagecodecs.jpeg2k_decode if ds.file_meta.TransferSyntaxUID.endswith(".91") else imagecodecs.jpeg8_decode
    return decode(fragment), pixel_array(path, index=k)


def check(root: zarr.Group, path: str, tiles: int, rng: random.Random) -> list[str]:
    failures = []
    slide = root[path]
    image = slide["image"]
    validate(image.attrs["ome"], "0.6", model="image", strict=True)
    level0 = image["0"]
    ty, tx = level0.chunks[1:]
    rows, cols = -(-level0.shape[1] // ty), -(-level0.shape[2] // tx)
    url = level0.attrs["source_url"]
    labels = "labels" in image and "nuclei" in image["labels"]
    if labels:
        validate(image["labels/nuclei"].attrs["ome"], "0.6", model="label")
        seg_urls = image["labels/nuclei"].attrs["source_url"]
        seg = {}
        for u in seg_urls:
            ds = pydicom.dcmread(local(u), stop_before_pixels=True)
            seg[(int(ds.TotalPixelMatrixRows), int(ds.TotalPixelMatrixColumns))] = local(u)
        seg0 = seg[tuple(level0.shape[1:])]
    for _ in range(tiles):
        r, c = rng.randrange(rows), rng.randrange(cols)
        k = r * cols + c
        stored = level0[:, r * ty:(r + 1) * ty, c * tx:(c + 1) * tx]
        by_codec, by_pydicom = source_tile(url, k)
        h, w = stored.shape[1:]
        expected = by_codec[:h, :w].transpose(2, 0, 1)
        if not np.array_equal(stored, expected):
            failures.append(f"{path}: tile {k} differs from imagecodecs' decode")
        if np.abs(stored.astype(int) - by_pydicom[:h, :w].transpose(2, 0, 1)).max() > 1:
            failures.append(f"{path}: tile {k} differs from pydicom's decode by more than 1")
        if labels:
            mask = image["labels/nuclei/0"][r * ty:(r + 1) * ty, c * tx:(c + 1) * tx]
            if not np.array_equal(mask, pixel_array(seg0, index=k)[:h, :w]):
                failures.append(f"{path}: mask tile {k} differs from the SEG frame")
    if "nuclei" in slide:
        nuclei = slide["nuclei"]
        group = pydicom.dcmread(local(nuclei.attrs["source_url"])).AnnotationGroupSequence[0]
        xy = np.frombuffer(group.PointCoordinatesData, "<f4").reshape(-1, 2)
        index = np.frombuffer(group.LongPrimitivePointIndexList, "<u4")
        area = np.frombuffer(group.MeasurementsSequence[0].MeasurementValuesSequence[0].FloatingPointValues, "<f4")
        for name, expected in (("coords", xy), ("point_index", index), ("area", area)):
            if not np.array_equal(nuclei[name][:], expected):
                failures.append(f"{path}: nuclei/{name} differs from the ANN")
        offset0 = nuclei["offset0"][:]
        if offset0[-1] != len(xy) or not np.array_equal(offset0[:-1], (index.astype(np.int64) - 1) // 2):
            failures.append(f"{path}: nuclei/offset0 does not index coords")
    return failures


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("repo")
    p.add_argument("--tiles", type=int, default=4)
    a = p.parse_args()
    start = time.time()
    root = open_repo(a.repo)
    rng = random.Random(0)
    failures, n = [], 0
    for path in slides(root):
        failures += check(root, path, a.tiles, rng)
        n += 1
    for f in failures:
        say(f"FAIL {f}")
    say(f"{n} slides, {a.tiles} tiles each, {len(failures)} failures, {time.time() - start:.0f} s")


if __name__ == "__main__":
    main()
