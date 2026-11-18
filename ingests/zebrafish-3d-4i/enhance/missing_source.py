"""Mark the source's damaged chunks with a label image on every image (PLAN.md decision 18).

What the source lost
--------------------
The source stores each array in chunks of 10 planes by 1000 x 1000 pixels of
the well image, at every pyramid level. 1,244 of the zip's chunk members are
empty files, and a few more hold Blosc frames cut short. No reader can decode
either kind, so the copy reads them as 0 (decision 13). 585 of them fall
inside some field, and each image lists the ones inside it under
``annotations.empty_source_chunks`` and ``annotations.unreadable_source_chunks``.

A list in the attributes is easy to miss, and a viewer cannot draw it. This
script turns the lists into ``labels/missing_source``, a label image a viewer
can overlay on the image: every voxel the damage set to 0 carries a nonzero
label value, and every other voxel is 0.

How the label is laid out
-------------------------
Each damaged chunk covers a block of 10 planes and up to 1000 x 1000 pixels
of the field, its *footprint*. Each footprint gets its own label value, so
in a viewer one damaged chunk is one object. ``image-label.properties`` says
for each value which level it is at, which array and channel lost data, the
source member and why it could not be read. Three footprints are shared by
two damaged members (a channel and a label at the same chunk position); their
value lists both, separated by "; ".

Level k of the label marks the damage in the source's level k. Each source
level was stored and damaged independently, so a damaged chunk at level 2
zeroes level 2 only, and the label's levels are not downsamplings of one
another. Each level carries its image's scale and translation, so a label
pixel lies on the image pixel it describes.

The label has the image's levels, one (z, y, x) array each, with the label
arrays' chunks of one plane and one shard over all planes. Images without
damage get the label too, all 0, so every image has the same hierarchy;
chunks that are all 0 are not written, so those labels cost only metadata.

What it touches
---------------
New groups and arrays under each image's ``labels``, and each ``labels``
group's list of names. No existing array or chunk changes. Before
committing, the script reads back:

- every damaged image's label at every level: each footprint holds its value,
  and no other voxel is nonzero;
- the image itself under one level-0 footprint per well: the damaged channel
  is 0 there, as decision 13 says it must be;
- the session's changes, which must be exactly the new nodes.

Run ``--dry-run`` to do all of that without committing.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import zarr

sys.path.insert(0, str(Path(__file__).parent))
from change import REPO, change, check_session, code_ref, enhance_session, image_paths  # noqa: E402

from minus80.core.icechunk_io import arraylake_repository, commit  # noqa: E402
from minus80.ome import label_attrs, labels_attrs  # noqa: E402

NAME = "missing_source"
DIMS = ("z", "y", "x")
#: The source's chunk at every level: 10 planes, 1000 x 1000 pixels of the well image.
SOURCE_CHUNK = (10, 1000, 1000)
PIXEL_UM = 0.65


@dataclass
class Footprint:
    """One damaged source chunk position at one level, and the members that lost it."""

    level: int
    index: tuple[int, int, int]
    arrays: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def footprints(annotations: dict) -> list[Footprint]:
    """The image's damaged chunks, grouped by level and chunk position.

    A member name is a Zarr key of the source: ``B/02/2/0/3/21/3/11`` is
    chunk (c=3, z=21, y=3, x=11) of level 0 of the round-2 well image
    ``B/02/2``, and ``B/02/1/labels/cells/3/10/0/1`` is chunk (10, 0, 1) of
    level 3 of the ``cells`` label.
    """
    channel_names = {
        (c["source_image"], c["source_channel"]): c["label"] for c in annotations["channels"]
    }
    damaged = [(m, "empty member") for m in annotations["empty_source_chunks"]]
    damaged += list(annotations["unreadable_source_chunks"].items())
    grouped: dict[tuple[int, tuple[int, int, int]], Footprint] = {}
    for member, reason in damaged:
        parts = member.split("/")
        source_image = "/".join(parts[:3])
        if parts[3] == "labels":
            array, channel = f"labels/{parts[4]}", parts[4]
            level, index = int(parts[5]), tuple(int(p) for p in parts[6:9])
        else:
            level, c = int(parts[3]), int(parts[4])
            array, channel = "image", channel_names[(source_image, c)]
            index = tuple(int(p) for p in parts[5:8])
        spot = grouped.setdefault((level, index), Footprint(level, index))
        spot.arrays.append(array)
        spot.channels.append(channel)
        spot.members.append(member)
        spot.reasons.append(reason)
    return sorted(grouped.values(), key=lambda f: (f.level, f.index))


def level_transforms(attrs: dict) -> list[tuple[dict[str, float], dict[str, float]]]:
    """Each level's (scale, translation) by axis name, read from an image's multiscales."""
    multiscale = attrs["ome"]["multiscales"][0]
    axes = [a["name"] for a in multiscale["coordinateSystems"][0]["axes"]]
    out = []
    for dataset in multiscale["datasets"]:
        steps = {t["type"]: t for t in dataset["coordinateTransformations"][0]["transformations"]}
        out.append((dict(zip(axes, steps["scale"]["scale"])),
                    dict(zip(axes, steps["translation"]["translation"]))))
    return out


def window_slices(spot: Footprint, field_origin: tuple[int, int], level_shape: tuple[int, ...],
                  where: str) -> tuple[slice, slice, slice]:
    """Where a footprint lies in the field's array at its level.

    The field's level-k window starts at its level-0 origin divided by 2^k,
    rounded down (``minus80.ome.crop``), and the source chunk covers
    ``index * SOURCE_CHUNK`` of the well image at that level.
    """
    f = 2**spot.level
    starts = (0, field_origin[0] // f, field_origin[1] // f)
    out = []
    for axis, (i, size, start, extent) in enumerate(zip(spot.index, SOURCE_CHUNK, starts, level_shape)):
        lo, hi = max(i * size - start, 0), min((i + 1) * size - start, extent)
        if lo >= hi:
            raise SystemExit(f"{where}: footprint {spot.index} at level {spot.level} misses the field "
                             f"on axis {DIMS[axis]}")
        out.append(slice(lo, hi))
    return tuple(out)


def properties(spots: list[Footprint]) -> list[dict]:
    return [
        {
            "label-value": value,
            "level": spot.level,
            "array": "; ".join(spot.arrays),
            "channel": "; ".join(spot.channels),
            "source_member": "; ".join(spot.members),
            "reason": "; ".join(spot.reasons),
        }
        for value, spot in enumerate(spots, start=1)
    ]


def build(root: zarr.Group, path: str) -> tuple[list[Footprint], list[list[tuple[slice, ...]]], list[str]]:
    """Write one image's ``missing_source`` label; return its footprints, their slices and arrays written."""
    image = root[path]
    attrs = dict(image.attrs)
    annotations = attrs["annotations"]
    roi = annotations["FOV_ROI_table"]
    origin = (round(roi["y_micrometer"] / PIXEL_UM), round(roi["x_micrometer"] / PIXEL_UM))
    transforms = level_transforms(attrs)
    shapes = [image[str(k)].shape[1:] for k in range(len(transforms))]
    spots = footprints(annotations)
    slices = [window_slices(s, origin, shapes[s.level], path) for s in spots]

    if "labels" in image:
        labels = image["labels"]
        names = list(labels.attrs["ome"]["labels"])
        if NAME in names:
            raise SystemExit(f"{path}: already has labels/{NAME}")
        template = labels[names[0]]["0"]
    else:
        labels, names, template = image.create_group("labels"), [], None
    labels.attrs["ome"] = labels_attrs([*names, NAME])

    group = labels.create_group(NAME)
    written = []
    for k, shape in enumerate(shapes):
        array = group.create_array(
            str(k), shape=shape, dtype="uint16", fill_value=0,
            chunks=(1, *shape[1:]), shards=shape,
            compressors=template.compressors if template is not None else image[str(k)].compressors,
            dimension_names=DIMS,
        )
        mine = [(v, sl) for v, (s, sl) in enumerate(zip(spots, slices), start=1) if s.level == k]
        if mine:
            data = np.zeros(shape, dtype="uint16")
            for value, sl in mine:
                data[sl] = value
            array[...] = data
            written.append(f"{path}/labels/{NAME}/{k}")

    scales = [{d: s[d] for d in DIMS} for s, _ in transforms]
    translations = [{d: t[d] for d in DIMS} for _, t in transforms]
    group.attrs.update({
        "ome": label_attrs(
            name=NAME, dims=DIMS, level_shapes=shapes, scale=scales[0],
            units={d: "micrometer" for d in DIMS},
            description="Voxels the source could not supply: each nonzero value is one damaged "
                        "source chunk at this level, described in image-label.properties.",
            level_scales=scales, level_translations=translations,
            properties=properties(spots) or None,
        ),
        "annotations": {
            "derived_from": ["annotations.empty_source_chunks", "annotations.unreadable_source_chunks"],
            "levels": "level k marks damage in the source's level k only",
        },
    })
    return spots, slices, written


def check_label(root: zarr.Group, path: str, spots: list[Footprint], slices: list) -> None:
    group = root[f"{path}/labels/{NAME}"]
    for k in range(len(group.attrs["ome"]["multiscales"][0]["datasets"])):
        mine = [(v, sl) for v, (s, sl) in enumerate(zip(spots, slices), start=1) if s.level == k]
        if not mine:
            continue
        data = group[str(k)][...]
        expected = sum(int(np.prod([s.stop - s.start for s in sl])) for _, sl in mine)
        for value, sl in mine:
            if not (data[sl] == value).all():
                raise SystemExit(f"{path} level {k}: value {value} does not fill its footprint")
        if int(np.count_nonzero(data)) != expected:
            raise SystemExit(f"{path} level {k}: {np.count_nonzero(data)} labeled voxels, expected {expected}")


def check_image_zero(root: zarr.Group, path: str, spots: list[Footprint], slices: list) -> str | None:
    """The image's damaged channel under one level-0 footprint must read as 0."""
    channels = [c["label"] for c in root[path].attrs["annotations"]["channels"]]
    for spot, sl in zip(spots, slices):
        if spot.level == 0 and spot.arrays == ["image"]:
            c = channels.index(spot.channels[0])
            pixels = root[f"{path}/0"][(c, *sl)]
            if pixels.any():
                raise SystemExit(f"{path} {spot.members[0]}: {np.count_nonzero(pixels)} nonzero pixels")
            return f"{path} {spot.channels[0]} under {spot.members[0]}: {pixels.size} pixels, all 0"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="check everything, commit nothing")
    args = parser.parse_args()

    here = Path(__file__)
    code = None if args.dry_run else code_ref(here, here.with_name("change.py"))
    repo = arraylake_repository(REPO)
    session = enhance_session(repo)
    root = zarr.open_group(session.store, mode="r+")

    paths = image_paths(root)
    had_labels = {p for p in paths if "labels" in root[p]}
    built, written = {}, []
    for n, path in enumerate(paths, start=1):
        spots, slices, arrays = build(root, path)
        built[path] = (spots, slices)
        written += arrays
        if n % 20 == 0 or n == len(paths):
            print(f"built {n} of {len(paths)} images, {len(written)} arrays with damage so far", flush=True)

    reread = zarr.open_group(session.store, mode="r")
    for path, (spots, slices) in built.items():
        check_label(reread, path, spots, slices)
    zero_lines, wells_seen = [], set()
    for path, (spots, slices) in built.items():
        well = path.rsplit("/", 1)[0]
        if well not in wells_seen and (line := check_image_zero(reread, path, spots, slices)):
            wells_seen.add(well)
            zero_lines.append(line)
    levels = len(level_transforms(dict(root[paths[0]].attrs)))
    check_session(
        session,
        updated_groups={f"{p}/labels" for p in had_labels},
        new_groups={f"{p}/labels/{NAME}" for p in paths} | {f"{p}/labels" for p in set(paths) - had_labels},
        new_arrays={f"{p}/labels/{NAME}/{k}" for p in paths for k in range(levels)},
        chunks_under=set(written),
    )
    shards = sum(len(v) for v in session.status().updated_chunks.values())

    count = sum(len(s) for s, _ in built.values())
    damaged = sum(1 for s, _ in built.values() if s)
    by_level = defaultdict(int)
    for spots, _ in built.values():
        for s in spots:
            by_level[s.level] += 1
    checked = (
        f"Read back before committing: in each of the {damaged} damaged images, every level's "
        f"{NAME} holds each footprint's value over the whole footprint and is 0 elsewhere; "
        f"under one level-0 footprint in each of {len(zero_lines)} wells the damaged image "
        f"channel reads as 0; the session added exactly the {len(paths)} label groups and "
        f"their {len(paths) * levels} arrays, changed only the labels lists, and wrote "
        f"chunks only into the {len(written)} arrays that mark damage."
    )
    print("\n".join(zero_lines))
    print(checked)
    print(f"{count} footprints by level: {dict(sorted(by_level.items()))}; {shards} shards written")
    if args.dry_run:
        print("dry run: nothing committed")
        return

    message = (
        f"Mark the source's damaged chunks with a {NAME} label on every image\n\n"
        "585 source chunks inside the fields are empty files or truncated Blosc frames, which "
        "the copy reads as 0 and lists in each image's annotations. Each image now has "
        f"labels/{NAME}, a label image with one value per damaged chunk position; "
        "image-label.properties gives each value's level, array, channel, source member and "
        "reason. Level k marks damage in the source's level k only. Images without damage "
        "get the label all 0, which stores no chunks (PLAN.md decision 18).\n\n" + checked
    )
    metadata = change(
        plan_decision=18,
        summary=f"Every image has labels/{NAME}, one label value per damaged source chunk.",
        reason="The damaged chunks were listed only in attributes, which a viewer cannot draw.",
        kind="pixels",
        scope={"pattern": f"*/*/*/labels/{NAME}", "count": len(paths)},
        chunks_written=shards,
        details={"footprints": count, "footprints_by_level": {str(k): v for k, v in sorted(by_level.items())},
                 "images_with_damage": damaged, "dtype": "uint16"},
        checked=checked,
        code=code,
    )
    snapshot = commit(session, message, metadata=metadata)
    print(f"committed {snapshot} on enhance")


if __name__ == "__main__":
    main()
