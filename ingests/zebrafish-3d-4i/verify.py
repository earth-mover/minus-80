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
- ``images``: each well holds one image per field, ``FOV_n`` at path ``n - 1``, whose
  channels are every channel of every round in round order, with the source's
  ROI row and every label of the field;
- ``scene``: each well's scene is valid 0.6 and places each image at its
  field's position in the source table;
- ``coverage``: each level covers exactly the coarse pixels its field touches;
- ``layout``: chunks, shards and codec are as PLAN.md decides;
- ``rounds listed``: the plate records every round and its channels, including
  round 3, which the source's plate leaves out;
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
from minus80.parsers.fractal.plate import FIELDS_TABLE, TABLES, read_table
from minus80.parsers.zip.members import zip_index
from minus80.parsers.zip.store import MemberStore, members_in_memory, metadata_members


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
            members_in_memory(self.url, metadata_members(self.index, also=[TABLES])), mode="r"
        )

    @cached_property
    def pixels(self) -> zarr.Group:
        return zarr.open_group(MemberStore(self.url, self.index), mode="r")

    @cached_property
    def wells(self) -> list[str]:
        return [w["path"] for w in dict(self.plan.attrs)["plate"]["wells"]]

    def rounds(self, well: str) -> list[int]:
        return sorted(int(r) for r in self.plan[well].group_keys() if r.isdigit())

    def channels(self, well: str) -> list[tuple[str, int, int]]:
        """Every channel of every round as ``(name, round, position in that round)``, in round order."""
        return [
            (channel["label"], rnd, position)
            for rnd in self.rounds(well)
            for position, channel in enumerate(
                self.plan[f"{well}/{rnd}"].attrs["omero"]["channels"]
            )
        ]

    def labels(self, well: str) -> dict[str, int]:
        """Each label's name, with the round that holds it."""
        return {
            name: rnd
            for rnd in self.rounds(well)
            if "labels" in self.plan[f"{well}/{rnd}"]
            for name in self.plan[f"{well}/{rnd}/labels"].attrs["labels"]
        }

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
    """Each well holds one image per source field, with every round's channels in order, and nothing else."""
    problems = []
    for well in store_wells(root):
        rounds = ref.rounds(well)
        expected = {image_of(name): name for name in ref.fields(well, rounds[0])}
        listed = {i["path"] for i in well_images(root, well)}
        if listed != set(expected):
            problems.append(
                f"{well}: images {sorted(listed)}, expected {sorted(expected)} "
                f"for fields {sorted(expected.values())}"
            )
            continue
        channels = ref.channels(well)
        names = [label for label, _, _ in channels]
        labels = ref.labels(well)
        for path, name in sorted(expected.items()):
            image = root[f"{well}/{path}"]
            attrs = dict(image.attrs)
            notes = attrs["annotations"]
            stored = [c["label"] for c in attrs["ome"]["omero"]["channels"]]
            if stored != names:
                problems.append(
                    f"{well}/{path}: channels {stored}, the source's rounds give {names}"
                )
            recorded = [(c["label"], c["round"], c["source_channel"]) for c in notes["channels"]]
            if recorded != channels:
                problems.append(
                    f"{well}/{path}: channel annotations differ from the source's rounds"
                )
            if image["0"].shape[0] != len(channels):
                problems.append(
                    f"{well}/{path}: {image['0'].shape[0]} channels, expected {len(channels)}"
                )
            if notes["field"] != name:
                problems.append(f"{well}/{path}: annotated as field {notes['field']}")
            if notes[FIELDS_TABLE] != ref.fields(well, rounds[0])[name]:
                problems.append(
                    f"{well}/{path}: ROI annotation differs from the source table's row"
                )
            got = list(dict(image["labels"].attrs)["ome"]["labels"]) if "labels" in image else []
            if got != list(labels):
                problems.append(f"{well}/{path}: labels {got}, the source has {list(labels)}")
    return problems


def image_of(field_name: str) -> str:
    """The image path the layout gives source field ``FOV_n``: ``n - 1``."""
    return str(int(field_name.removeprefix("FOV_")) - 1)


def source_row(ref: Reference, well: str, path: str) -> dict:
    """The source ``FOV_ROI_table`` row of the field stored at image ``path``."""
    return ref.fields(well, ref.rounds(well)[0])[f"FOV_{int(path) + 1}"]


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
            roi = source_row(ref, well, path)
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

    In the source well image, level-0 pixel ``i`` has its center at ``i * p``,
    and a coarse pixel ``j`` of size ``s`` stands for the level-0 pixels from
    ``j * s / p``, so its center is at ``(j + 1/2) * s - p / 2``. The level's
    first pixel center is its translation plus the field's origin, so

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
    roi = source_row(ref, well, path)
    (ms,) = dict(image.attrs)["ome"]["multiscales"]
    fine, _ = level_transforms(ms["datasets"][0])
    for dataset in ms["datasets"]:
        array = image[dataset["path"]]
        yield dataset, array, level_window(dataset, fine, roi, array.shape[-2:]), roi


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
        well = name.rsplit("/", 1)[0]
        for dataset, _, window, roi in levels_of(root, ref, name):
            scale, _ = level_transforms(dataset)
            fine, _ = level_transforms(
                dict(root[name].attrs)["ome"]["multiscales"][0]["datasets"][0]
            )
            extent = ref.plan[f"{well}/{ref.rounds(well)[0]}/{dataset['path']}"].shape[-2:]
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
    """The plate records every round the source's wells hold, with its channels.

    This includes any round the source's own plate metadata leaves out.
    """
    well = ref.wells[0]
    want = {
        str(rnd): [label for label, r, _ in ref.channels(well) if r == rnd]
        for rnd in ref.rounds(well)
    }
    got = dict(root.attrs)["source"]["rounds"]
    return [] if got == want else [f"plate records rounds {got}, the source holds {want}"]


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
    """Every channel and label equals the source at every level, on a sample of images or all of them.

    With ``sample=None`` every plane of every image is compared. Otherwise
    ``sample`` images are compared, on the planes either side of each shard
    boundary.
    """
    problems = []
    names = field_images(root)
    if sample is not None:
        names = random.Random(seed).sample(names, min(sample, len(names)))
    for name in names:
        well = name.rsplit("/", 1)[0]
        image = root[name]
        for dataset, array, (y0, y1, x0, x1), _ in levels_of(root, ref, name):
            level = dataset["path"]
            planes = planes_to_compare(array.shape[1], shard_depth, every=sample is None)
            for stored, (label, rnd, position) in enumerate(ref.channels(well)):
                source = ref.pixels[f"{well}/{rnd}/{level}"]
                want = source[position, planes, y0:y1, x0:x1]
                if not np.array_equal(array[stored, planes], want):
                    problems.append(
                        f"{name}/{level} channel {stored} ({label}): differs from "
                        f"{well}/{rnd}/{level} channel {position}"
                    )
            for label, rnd in ref.labels(well).items():
                want = ref.pixels[f"{well}/{rnd}/labels/{label}/{level}"][planes, y0:y1, x0:x1]
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
