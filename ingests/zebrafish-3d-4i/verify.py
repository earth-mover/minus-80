"""Check a converted 3D-4i zebrafish store against the zip it came from.

Every check compares the store with the source, never the store with itself:

- **What should be there** comes from the source: its plate's wells, the
  rounds each well holds, and each round's ``FOV_ROI_table``. The annotations
  the converter wrote are checked against these, not trusted.
- **Where each level's pixels should come from** is worked out from the
  store's own metadata (each level's scale and translation) and the field's
  row in the source table. A pixel comparison therefore checks that metadata
  as well as the pixels.

The checks, in the order they run:

- the archive-independent gates from ``minus80.ome.verify``, on every image;
- ``wells``: the plate lists every well of the source, in the source's order;
- ``images``: each well holds one image per field and round, at the path the
  layout gives it, tagged with the right round and the source's ROI row;
- ``scene``: each well's scene is valid 0.6 and places each image at its
  field's position in the source table;
- ``coverage``: each level covers exactly the coarse pixels its field touches;
- ``layout``: chunks, shards and codec are as PLAN.md decides;
- ``rounds listed``: the plate lists every round, including round 3, which
  the source's plate leaves out;
- ``pixels``: pixels and labels equal the source's at every level, on a sample
  of images and the planes either side of each shard boundary, or on every
  plane of every image with ``--all``.

    uv run --package zebrafish-3d-4i-ingest python ingests/zebrafish-3d-4i/verify.py \\
        --repo <dir | arraylake://org/repo> [--source <zip URL>] [--all]
"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property

import ingest
import numpy as np
import zarr

from minus80.ome.hierarchy import images
from minus80.ome.image import validate
from minus80.ome.verify import GATES, run_gates
from minus80.parsers.fractal.plate import FIELDS_TABLE, read_table
from minus80.parsers.zip.members import zip_index
from minus80.parsers.zip.store import MemberStore


@dataclass
class Reference:
    """The source plate, read through an index taken fresh from the zip, not the converter's cache."""

    url: str
    tables: dict = field(default_factory=dict)

    @cached_property
    def index(self):
        return zip_index(self.url)

    @cached_property
    def plan(self) -> zarr.Group:
        """The plate's metadata and tables, cheap to list."""
        return zarr.open_group(
            MemberStore(self.url, ingest.metadata_members(self.index)), mode="r", zarr_format=2
        )

    @cached_property
    def pixels(self) -> zarr.Group:
        return zarr.open_group(MemberStore(self.url, self.index), mode="r", zarr_format=2)

    @cached_property
    def wells(self) -> list[str]:
        return [w["path"] for w in dict(self.plan.attrs)["plate"]["wells"]]

    def rounds(self, well: str) -> list[int]:
        return sorted(int(r) for r in self.plan[well].group_keys() if r.isdigit())

    def fields(self, well: str, rnd: int) -> dict[str, dict]:
        """The round's ``FOV_ROI_table``, as each field's row by field name."""
        if (well, rnd) not in self.tables:
            table = read_table(self.plan[f"{well}/{rnd}/tables/{FIELDS_TABLE}"])
            self.tables[(well, rnd)] = {
                str(i): {k: float(v) for k, v in row.items()} for i, row in table.iterrows()
            }
        return self.tables[(well, rnd)]


def store_wells(root) -> list[str]:
    return [w["path"] for w in dict(root.attrs)["ome"]["plate"]["wells"]]


def well_images(root, well) -> list[dict]:
    return dict(root[well].attrs)["ome"]["well"]["images"]


def wells(root, ref: Reference) -> list[str]:
    """The plate lists every well of the source, in the source's order."""
    got = store_wells(root)
    return (
        []
        if got == ref.wells
        else [
            f"plate lists {len(got)} wells, the source {len(ref.wells)}: "
            f"missing {sorted(set(ref.wells) - set(got))}"
        ]
    )


def images_match(root, ref: Reference) -> list[str]:
    """Each well holds image ``rounds * (n - 1) + r`` for field ``FOV_n`` in round ``r``, and nothing else."""
    problems = []
    for well in store_wells(root):
        rounds = ref.rounds(well)
        expected = {}
        for rnd in rounds:
            for name in ref.fields(well, rnd):
                n = int(name.removeprefix("FOV_"))
                expected[str(len(rounds) * (n - 1) + rounds.index(rnd))] = (name, rnd)
        listed = {i["path"]: i.get("acquisition") for i in well_images(root, well)}
        if set(listed) != set(expected):
            problems.append(f"{well}: images {sorted(listed)}, expected {sorted(expected)}")
            continue
        for path, (name, rnd) in expected.items():
            notes = dict(root[f"{well}/{path}"].attrs)["annotations"]
            if listed[path] != rnd:
                problems.append(f"{well}/{path}: acquisition {listed[path]}, expected round {rnd}")
            if (notes["field"], notes["round"], notes["source_image"]) != (
                name,
                rnd,
                f"{well}/{rnd}",
            ):
                problems.append(
                    f"{well}/{path}: annotated as {notes['field']} round {notes['round']}, "
                    f"expected {name} round {rnd}"
                )
            if notes[FIELDS_TABLE] != ref.fields(well, rnd)[name]:
                problems.append(
                    f"{well}/{path}: ROI annotation differs from the source table's {name} row"
                )
            source = ref.plan[f"{well}/{rnd}"]
            if "labels" in source:
                want = list(source["labels"].attrs["labels"])
                got = list(dict(root[f"{well}/{path}/labels"].attrs)["ome"]["labels"])
                if got != want:
                    problems.append(f"{well}/{path}: labels {got}, the source has {want}")
    return problems


def source_row(root, ref: Reference, well: str, path: str) -> tuple[dict, str]:
    """The image's field row in the source table, and its source image path, from its path alone."""
    rounds = ref.rounds(well)
    n, r = divmod(int(path), len(rounds))
    rnd = rounds[r]
    return ref.fields(well, rnd)[f"FOV_{n + 1}"], f"{well}/{rnd}"


def scene(root, ref: Reference) -> list[str]:
    """Each well's scene is valid 0.6 and moves each image to its field's position in the source table."""
    problems = []
    for well in store_wells(root):
        ome = dict(root[well].attrs)["ome"]
        try:
            validate(ome, ingest.VERSION, model="scene")
        except Exception as e:
            problems.append(f"{well}: scene does not validate: {e}"[:200])
            continue
        moves = {t["input"]["path"]: t for t in ome["scene"]["coordinateTransformations"]}
        paths = [i["path"] for i in well_images(root, well)]
        if set(moves) != set(paths):
            problems.append(f"{well}: scene places {sorted(moves)}, the well lists {sorted(paths)}")
            continue
        for path in paths:
            roi, _ = source_row(root, ref, well, path)
            want = [0.0, roi["z_micrometer"], roi["y_micrometer"], roi["x_micrometer"]]
            if (
                list(moves[path]["translation"]) != want
                or moves[path]["output"]["name"] != ingest.SCENE
            ):
                problems.append(
                    f"{well}/{path}: scene translation {moves[path]['translation']}, source ROI {want}"
                )
    return problems


def level_transforms(dataset: dict) -> tuple[list[float], list[float]]:
    """A level's scale and translation, from a 0.6 scale or scale-then-translation sequence."""
    (transform,) = dataset["coordinateTransformations"]
    steps = transform["transformations"] if transform["type"] == "sequence" else [transform]
    scale = next(t["scale"] for t in steps if t["type"] == "scale")
    shift = next(
        (list(t["translation"]) for t in steps if t["type"] == "translation"), [0.0] * len(scale)
    )
    return scale, shift


def level_window(dataset: dict, fine: list[float], roi: dict, shape) -> tuple[int, int, int, int]:
    """The level's ``(y0, y1, x0, x1)`` in the source level, from its stored scale and translation.

    In the source well image, level-0 pixel ``i`` has its centre at ``i * p``,
    and a coarse pixel ``j`` of size ``s`` stands for the level-0 pixels from
    ``j * s / p``, so its centre is at ``(j + 1/2) * s - p / 2``. The level's
    first pixel centre is its translation plus the field's origin, so

        j = (translation + origin + p / 2) / s - 1/2

    which must come out a whole number.
    """
    scale, shift = level_transforms(dataset)
    starts = []
    for axis, origin in ((2, roi["y_micrometer"]), (3, roi["x_micrometer"])):
        j = (shift[axis] + origin + fine[axis] / 2) / scale[axis] - 0.5
        if abs(j - round(j)) > 1e-6:
            raise ValueError(
                f"level {dataset['path']}: translation {shift[axis]} puts its first pixel "
                f"at {j} source pixels, not a whole one"
            )
        starts.append(round(j))
    return starts[0], starts[0] + shape[0], starts[1], starts[1] + shape[1]


def levels_of(root, ref: Reference, name: str):
    """Each level of image ``name`` with its window into the source, from its metadata and the source ROI."""
    well, path = name.rsplit("/", 1)
    image = root[name]
    roi, source_path = source_row(root, ref, well, path)
    (ms,) = dict(image.attrs)["ome"]["multiscales"]
    fine, _ = level_transforms(ms["datasets"][0])
    for dataset in ms["datasets"]:
        array = image[dataset["path"]]
        yield dataset, array, level_window(dataset, fine, roi, array.shape[-2:]), roi, source_path


def field_images(root) -> list[str]:
    return sorted(n for n in images(root) if "/labels/" not in n)


def coverage(root, ref: Reference) -> list[str]:
    """Each level covers exactly the coarse pixels its field touches, as far as the source level goes.

    In the level's own pixels, with ``f`` level-0 pixels to one of its own and
    the field spanning level-0 pixels ``o`` to ``o + n``: the first pixel must
    hold level-0 pixel ``o``, the last must hold ``o + n - 1``, unless the
    source level ends first.
    """
    problems = []
    for name in field_images(root):
        for dataset, _, window, roi, source_path in levels_of(root, ref, name):
            scale, _ = level_transforms(dataset)
            fine, _ = level_transforms(
                dict(root[name].attrs)["ome"]["multiscales"][0]["datasets"][0]
            )
            extent = ref.plan[f"{source_path}/{dataset['path']}"].shape[-2:]
            for i, axis in enumerate("yx"):
                start, end = window[2 * i], window[2 * i + 1]
                f = round(scale[2 + i] / fine[2 + i])
                o = round(roi[f"{axis}_micrometer"] / fine[2 + i])
                n = round(roi[f"len_{axis}_micrometer"] / fine[2 + i])
                if start != o // f or end != min(-(-(o + n) // f), extent[i]):
                    problems.append(
                        f"{name}/{dataset['path']}: {axis} covers source pixels {start}:{end} "
                        f"of {extent[i]}, the field spans level-0 {o}:{o + n}"
                    )
    return problems


def layout(root, shard_depth: int) -> list[str]:
    """Chunks are one plane of one channel; shards ``shard_depth`` planes, all planes for labels; blosc zstd 5."""
    problems = []
    for name, image in images(root).items():
        is_label = "/labels/" in name
        for key, array in image.arrays():
            chunks = (1, *array.shape[1:]) if is_label else (1, 1, *array.shape[2:])
            shards = array.shape if is_label else (1, shard_depth, *array.shape[2:])
            if array.chunks != chunks or array.shards != shards:
                problems.append(
                    f"{name}/{key}: chunks {array.chunks} in shards {array.shards}, "
                    f"expected {chunks} in {shards}"
                )
            codec = array.compressors[0] if array.compressors else None
            if codec is None or (
                codec_setting(codec.cname),
                codec.clevel,
                codec_setting(codec.shuffle),
            ) != ("zstd", 5, "bitshuffle"):
                problems.append(f"{name}/{key}: compressor {codec}")
    return problems


def codec_setting(setting) -> str:
    """A codec setting as text: zarr gives some as enums and some as strings."""
    return getattr(setting, "value", setting)


def rounds_listed(root, ref: Reference) -> list[str]:
    """The plate lists every round the source's wells hold, including any the source's plate leaves out."""
    held = ref.rounds(ref.wells[0])
    listed = sorted(a["id"] for a in dict(root.attrs)["ome"]["plate"]["acquisitions"])
    return (
        []
        if listed == held
        else [f"plate lists acquisitions {listed}, the wells hold rounds {held}"]
    )


def planes_to_compare(depth: int, shard_depth: int, every: bool) -> list[int]:
    """Every plane, or the first, the last and those either side of each shard boundary."""
    if every:
        return list(range(depth))
    edges = {0, depth - 1}
    for z in range(shard_depth, depth, shard_depth):
        edges |= {z - 1, z}
    return sorted(edges)


def pixels(
    root, ref: Reference, *, sample: int | None, shard_depth: int, seed: int = 0
) -> list[str]:
    """Pixels and labels equal the source's at every level, on a sample of images or (``sample=None``) all."""
    problems = []
    names = field_images(root)
    if sample is not None:
        names = random.Random(seed).sample(names, min(sample, len(names)))
    for name in names:
        image = root[name]
        for dataset, array, (y0, y1, x0, x1), _, source_path in levels_of(root, ref, name):
            level = dataset["path"]
            planes = planes_to_compare(array.shape[1], shard_depth, every=sample is None)
            want = ref.pixels[f"{source_path}/{level}"][:, planes, y0:y1, x0:x1]
            if not np.array_equal(array[:, planes], want):
                problems.append(
                    f"{name}/{level}: differs from {source_path}/{level}[{y0}:{y1}, {x0}:{x1}]"
                )
            if "labels" in image:
                for label in image["labels"].group_keys():
                    want = ref.pixels[f"{source_path}/labels/{label}/{level}"][planes, y0:y1, x0:x1]
                    if not np.array_equal(image[f"labels/{label}/{level}"][planes], want):
                        problems.append(f"{name}/labels/{label}/{level}: differs from the source")
    return problems


def gates(ref: Reference, shard_depth: int, sample: int | None) -> dict[str, Callable]:
    table = dict(GATES)
    table["wells"] = lambda root, n: wells(root, ref)
    table["images"] = lambda root, n: images_match(root, ref)
    table["scene"] = lambda root, n: scene(root, ref)
    table["coverage"] = lambda root, n: coverage(root, ref)
    table["layout"] = lambda root, n: layout(root, shard_depth)
    table["rounds listed"] = lambda root, n: rounds_listed(root, ref)
    table["pixels"] = lambda root, n: pixels(root, ref, sample=sample, shard_depth=shard_depth)
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
    root = zarr.open_group(
        ingest.open_repository(args.repo).readonly_session(args.branch).store, mode="r"
    )
    failed = run_gates(
        root,
        gates(Reference(args.source), args.shard_depth, None if args.all else args.sample),
        {"version": ingest.VERSION, "sample": 3},
    )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
