"""Check a converted 3D-4i zebrafish store against the zip it came from.

The source is read through zarr, from an index of the zip read fresh from its
central directory rather than the converter's cached one. Where each level's
pixels should come from is worked out from the store's own metadata, the
field's ``FOV_ROI_table`` row and each level's translation, not from the
converter's arithmetic, so a pixel comparison checks that metadata too.

The archive-independent gates run on every image. The archive's own gates
are: every field of every well and round is present; each well's scene places
each image where its ROI row says; each level covers exactly the coarse
pixels its field touches; arrays are chunked, sharded and compressed
as PLAN.md decides; the plate lists every round; and, on a sample, pixels and
labels equal the source's at every level, on the planes either side of each
shard boundary.

    uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/verify.py \\
        --repo <dir | arraylake://org/repo> [--source <zip URL>] [--all]
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Callable

import numpy as np
import zarr
from minus80.parsers.zip.members import zip_index
from minus80.parsers.zip.store import MemberStore
from minus80.ome.hierarchy import images
from minus80.ome.verify import GATES, run_gates

import ingest

def reference(url: str) -> zarr.Group:
    """The source plate, through an index read fresh from the zip's central directory."""
    return zarr.open_group(MemberStore(url, zip_index(url)), mode="r", zarr_format=2)


def _wells(root) -> dict[str, list[str]]:
    """Each well the plate lists, with the image paths its well metadata lists."""
    out = {}
    for w in dict(root.attrs)["ome"]["plate"]["wells"]:
        out[w["path"]] = [i["path"] for i in dict(root[w["path"]].attrs)["ome"]["well"]["images"]]
    return out


def complete(root, src) -> list[str]:
    """Every field of every round in the source is an image in the store, with its labels."""
    problems = []
    rounds = sorted(int(r) for r in src[next(iter(_wells(root)))].group_keys() if r.isdigit())
    for well, paths in _wells(root).items():
        table = ingest._table(src[f"{well}/{rounds[0]}/tables/{ingest.ROI_TABLE}"])
        expected = len(table) * len(rounds)
        if len(paths) != expected:
            problems.append(f"{well}: {len(paths)} images, the source has {len(table)} fields x {len(rounds)} rounds")
        for path in paths:
            notes = dict(root[f"{well}/{path}"].attrs)["annotations"]
            source = src[notes["source_image"]]
            if "labels" in source:
                want = list(source["labels"].attrs["labels"])
                got = list(dict(root[f"{well}/{path}/labels"].attrs)["ome"]["labels"])
                if got != want:
                    problems.append(f"{well}/{path}: labels {got}, the source has {want}")
    return problems


def scene(root) -> list[str]:
    """Each well's scene translates each image by its ``FOV_ROI_table`` position, the same for every round."""
    problems = []
    for well, paths in _wells(root).items():
        ome = dict(root[well].attrs)["ome"]
        moves = {t["input"]["path"]: t for t in ome["scene"]["coordinateTransformations"]}
        if set(moves) != set(paths):
            problems.append(f"{well}: scene places {sorted(moves)}, the well lists {sorted(paths)}")
            continue
        for path in paths:
            roi = dict(root[f"{well}/{path}"].attrs)["annotations"][ingest.ROI_TABLE]
            want = [0.0, roi["z_micrometer"], roi["y_micrometer"], roi["x_micrometer"]]
            if list(moves[path]["translation"]) != want or moves[path]["output"]["name"] != ingest.SCENE:
                problems.append(f"{well}/{path}: scene translation {moves[path]['translation']}, ROI {want}")
    return problems


def layout(root, shard_depth: int) -> list[str]:
    """Chunks are one plane of one channel, shards ``shard_depth`` planes (labels: all), blosc zstd 5 bitshuffle."""
    problems = []
    for name, image in images(root).items():
        for key, arr in image.arrays():
            problems += _layout(f"{name}/{key}", arr, (1, 1, *arr.shape[2:]), (1, shard_depth, *arr.shape[2:]))
        if "labels" in image:
            for label in image["labels"].group_keys():
                for key, arr in image[f"labels/{label}"].arrays():
                    problems += _layout(f"{name}/labels/{label}/{key}", arr, (1, *arr.shape[1:]), arr.shape)
    return problems


def _layout(where: str, arr, chunks, shards) -> list[str]:
    problems = []
    if arr.chunks != chunks or arr.shards != shards:
        problems.append(f"{where}: chunks {arr.chunks} in shards {arr.shards}, expected {chunks} in {shards}")
    codec = arr.compressors[0] if arr.compressors else None
    if codec is None or (_name(codec.cname), codec.clevel, _name(codec.shuffle)) != ("zstd", 5, "bitshuffle"):
        problems.append(f"{where}: compressor {codec}")
    return problems


def _name(setting) -> str:
    """A codec setting as text, whether zarr gives it as an enum or a string."""
    return getattr(setting, "value", setting)


def rounds_listed(root, src) -> list[str]:
    """The plate lists every round the source's wells hold, including any the source's plate leaves out."""
    held = sorted(int(r) for r in src[next(iter(_wells(root)))].group_keys() if r.isdigit())
    listed = sorted(a["id"] for a in dict(root.attrs)["ome"]["plate"]["acquisitions"])
    return [] if listed == held else [f"plate lists acquisitions {listed}, the wells hold rounds {held}"]


def pixels(root, src, *, sample: int | None, shard_depth: int, seed: int = 0) -> list[str]:
    """Pixels and labels equal the source's at every level, on a sample of images, or all with ``sample=None``.

    Each level's window into the source level is derived from the field's ROI
    origin and the level's stored scale and translation. With a sample, only
    the planes either side of each shard boundary are compared, plus the
    first and last; with ``sample=None``, every plane.
    """
    problems = []
    names = sorted(images(root))
    names = [n for n in names if "/labels/" not in n]
    if sample is not None:
        names = random.Random(seed).sample(names, min(sample, len(names)))
    for name in names:
        image = root[name]
        notes = dict(image.attrs)["annotations"]
        roi = notes[ingest.ROI_TABLE]
        source = src[notes["source_image"]]
        (ms,) = dict(image.attrs)["ome"]["multiscales"]
        fine, _ = _transforms(ms["datasets"][0])
        for dataset in ms["datasets"]:
            arr = image[dataset["path"]]
            window = _window(dataset, fine, roi, arr.shape[-2:])
            planes = _planes(arr.shape[1], shard_depth, every=sample is None)
            y0, y1, x0, x1 = window
            got = arr[:, planes]
            want = source[dataset["path"]][:, planes, y0:y1, x0:x1]
            if not np.array_equal(got, want):
                problems.append(f"{name}/{dataset['path']}: differs from {notes['source_image']}"
                                f"/{dataset['path']}[{y0}:{y1}, {x0}:{x1}]")
            if "labels" in image:
                for label in image["labels"].group_keys():
                    got = image[f"labels/{label}/{dataset['path']}"][planes]
                    want = source[f"labels/{label}/{dataset['path']}"][planes, y0:y1, x0:x1]
                    if not np.array_equal(got, want):
                        problems.append(f"{name}/labels/{label}/{dataset['path']}: differs from the source")
    return problems


def coverage(root, src) -> list[str]:
    """Each level covers exactly the coarse pixels its field touches, as far as the source level goes.

    In pixels of the level, with ``f`` level-0 pixels to one of its own and
    the field spanning level-0 pixels ``o`` to ``o + n``: the first pixel must
    start at or before ``o`` and end after it, the last must start before
    ``o + n``, and the last must end at or after ``o + n`` unless it is the
    source level's last pixel.
    """
    problems = []
    for name, image in images(root).items():
        if "/labels/" in name:
            continue
        notes = dict(image.attrs)["annotations"]
        roi = notes[ingest.ROI_TABLE]
        (ms,) = dict(image.attrs)["ome"]["multiscales"]
        fine, _ = _transforms(ms["datasets"][0])
        for dataset in ms["datasets"]:
            shape = image[dataset["path"]].shape[-2:]
            y0, y1, x0, x1 = _window(dataset, fine, roi, shape)
            scale, _ = _transforms(dataset)
            limit = src[f"{notes['source_image']}/{dataset['path']}"].shape[-2:]
            for axis, start, end, extent in ((2, y0, y1, limit[0]), (3, x0, x1, limit[1])):
                f = round(scale[axis] / fine[axis])
                o = round(roi[f"{'yx'[axis - 2]}_micrometer"] / fine[axis])
                n = round(roi[f"len_{'yx'[axis - 2]}_micrometer"] / fine[axis])
                first_ok = start * f <= o < (start + 1) * f
                last_ok = (end - 1) * f < o + n and (end * f >= o + n or end == extent)
                if not (first_ok and last_ok):
                    problems.append(f"{name}/{dataset['path']}: axis {'yx'[axis - 2]} covers source pixels "
                                    f"{start}:{end} of {extent}, the field spans level-0 {o}:{o + n}")
    return problems


def _transforms(dataset: dict) -> tuple[list[float], list[float]]:
    """A level's scale and translation, from a 0.6 scale or scale-then-translation sequence."""
    (transform,) = dataset["coordinateTransformations"]
    steps = transform["transformations"] if transform["type"] == "sequence" else [transform]
    scale = next(t["scale"] for t in steps if t["type"] == "scale")
    shift = next((list(t["translation"]) for t in steps if t["type"] == "translation"), [0.0] * len(scale))
    return scale, shift


def _window(dataset: dict, fine: list[float], roi: dict, shape) -> tuple[int, int, int, int]:
    """The level's ``(y0, y1, x0, x1)`` in the source level, from its stored scale and translation.

    In the source well image, level-0 pixel ``i`` has its centre at ``i * p``
    and a coarse pixel ``j`` of size ``s`` at ``(j + 1/2) * s - p / 2``. The
    level's first pixel centre sits at its translation plus the field's ROI
    origin, so ``j = (translation + origin + p / 2) / s - 1/2``, which must be
    a whole number.
    """
    scale, shift = _transforms(dataset)
    starts = []
    for axis, origin in ((2, roi["y_micrometer"]), (3, roi["x_micrometer"])):
        j = (shift[axis] + origin + fine[axis] / 2) / scale[axis] - 0.5
        if abs(j - round(j)) > 1e-6:
            raise ValueError(f"level {dataset['path']}: translation {shift[axis]} puts its first pixel "
                             f"at {j} source pixels, not a whole one")
        starts.append(round(j))
    return starts[0], starts[0] + shape[0], starts[1], starts[1] + shape[1]


def _planes(depth: int, shard_depth: int, *, every: bool) -> list[int]:
    if every:
        return list(range(depth))
    edges = {0, depth - 1}
    for z in range(shard_depth, depth, shard_depth):
        edges |= {z - 1, z}
    return sorted(edges)


def gates(src, shard_depth: int, sample: int | None) -> dict[str, Callable]:
    table = dict(GATES)
    table["complete"] = lambda root, n: complete(root, src)
    table["scene"] = lambda root, n: scene(root)
    table["coverage"] = lambda root, n: coverage(root, src)
    table["layout"] = lambda root, n: layout(root, shard_depth)
    table["rounds listed"] = lambda root, n: rounds_listed(root, src)
    table["pixels"] = lambda root, n: pixels(root, src, sample=sample, shard_depth=shard_depth)
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--source", default=ingest.SOURCE)
    parser.add_argument("--shard-depth", type=int, default=ingest.SHARD_DEPTH)
    parser.add_argument("--sample", type=int, default=8, help="images whose pixels are compared")
    parser.add_argument("--all", action="store_true", help="compare every plane of every image")
    args = parser.parse_args()
    repo = ingest.open_repository(args.repo)
    root = zarr.open_group(repo.readonly_session(args.branch).store, mode="r")
    failed = run_gates(root, gates(reference(args.source), args.shard_depth, None if args.all else args.sample),
                       {"version": ingest.VERSION, "sample": 3})
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
