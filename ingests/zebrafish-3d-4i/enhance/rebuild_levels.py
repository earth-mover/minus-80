"""Rebuild every image's levels 1 to 3 from its level 0 (PLAN.md decision 19).

Why the copied levels are replaced
----------------------------------
The source builds its pyramid over the whole well image, and each field
sits wherever the microscope put it. Most fields therefore start partway
through a coarse pixel. The copy kept the source's coarse pixels, so each
field's coarse levels are cut to every coarse pixel the field touches
(``minus80.ome.crop``):

- they are often a pixel wider than 1000 / 2^k (B/02/0 level 1 is
  500 x 501);
- the edge pixels average the field with the zero filler around it;
- each field carries its own translation at each level, and the labels'
  coarse levels carry yet another.

Rebuilt from the field's own level 0, every image's levels are exactly 500,
250 and 125 pixels wide, and one translation per level serves every image.

How the pixels are computed
---------------------------
The source's method, measured on the copy: each coarse image pixel is the
mean of a 2 x 2 block of the level above, rounded half up, so level 3 is
built from level 2, not from level 0 directly. On C/02/0, C/04/1 and C/05/3,
whose origins fall on a level-3 block boundary, that rule reproduces every
source pixel at levels 1 to 3 in 40 planes of three channels; rounding half
to even, or averaging level 0 directly, does not. This script uses the same
rule, so the rebuilt pixels are the ones the source would have made had it
built a pyramid per field. The mean is taken in integers, as
``(a + b + c + d + 2) // 4``, which is the same value.

The source's coarse labels keep the bottom-right pixel of each 2 x 2 block,
because a mean of label values is a meaningless third label. The rebuilt
labels keep the same pixel, and their translation places each coarse label
pixel on that kept pixel: ``(2^k - 1) x 0.65 um`` down and right of level 0's
first pixel, where an image level's block center is half that.

``missing_source`` is the exception. Its level 0 marks the voxels that
damaged level-0 source chunks set to 0. A coarse image pixel is affected
when any of the level-0 pixels it averages is, so its coarse levels take the
block's maximum instead of one pixel, and carry the image's translations.
The source's damage at levels 1 to 3 no longer affects any pixel, so those
label values and their properties are dropped; the image's
``annotations.empty_source_chunks`` and ``unreadable_source_chunks`` still
list them, because they describe the source.

Shards
------
Level 1 keeps 32-plane shards, about 14 MB compressed each. A level-2 or
level-3 shard of 32 planes would hold about 1 MB or 0.25 MB compressed, so
those levels get one shard over all 271 planes per channel: about 8.4 MB at
level 2 and 2.1 MB at level 3, measured on E/04/3. Every shard still holds
one-plane chunks, so a viewer reading one plane reads one chunk.

How it runs
-----------
Each (image, channel) and each (image, label) is one task: read level 0 a
32-plane shard at a time, halve it three times, write level 1 shard by
shard, and write levels 2 and 3 once the channel is complete. Level 0 is
read once and nothing else is decoded. Tasks run on a thread pool; NumPy,
Blosc and Icechunk release the GIL, so the pool keeps the cores and the
network busy together. All writes go into one session, committed once, so
the store's history has one commit for the rebuild.

Checks, before committing
-------------------------
- On every image whose origin falls on a level-3 block boundary (11 of
  211), each rebuilt level of every channel and label equals the source's,
  read from ``source-copy``, outside the chunks the source lost.
- On every image, one channel and one label at one plane are rebuilt again
  from a fresh read of level 0 and compared with what was written.
- Every image and label has the exact level shapes, the translations above,
  and metadata that validates against OME-Zarr 0.6.
- The session replaced exactly the coarse arrays and changed exactly the
  image and label groups.

Run ``--dry-run`` to do all of that without committing, and ``--images`` to
rebuild a few images only, which cannot be committed.
"""
from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import zarr

sys.path.insert(0, str(Path(__file__).parent))
from change import (  # noqa: E402
    PARENT_TAG, REPO, change, check_session, code_ref, enhance_session, image_paths,
)

from minus80.core.icechunk_io import arraylake_repository, commit  # noqa: E402
from minus80.core.progress import Heartbeat, StallWatch  # noqa: E402
from minus80.ome.image import validate  # noqa: E402

LEVELS = 4
PIXEL_UM = 0.65
#: Planes per level-0 and level-1 shard; levels 2 and 3 take every plane in one shard.
SHARD_PLANES = 32
MISSING = "missing_source"

IMAGE_METHOD = "Levels 1 to 3: each pixel is the mean of a 2 x 2 block of the level above, rounded half up."
LABEL_METHOD = "Levels 1 to 3: each pixel is the bottom-right pixel of a 2 x 2 block of the level above."
MISSING_METHOD = "Levels 1 to 3: each pixel is the largest value in a 2 x 2 block of the level above."


def halve_mean(a: np.ndarray) -> np.ndarray:
    """The 2 x 2 means of the last two axes, rounded half up, as the source computes them."""
    s = (a[..., 0::2, 0::2].astype(np.uint32) + a[..., 0::2, 1::2]
         + a[..., 1::2, 0::2] + a[..., 1::2, 1::2])
    return ((s + 2) // 4).astype(a.dtype)


def halve_pick(a: np.ndarray) -> np.ndarray:
    """The bottom-right pixel of each 2 x 2 block, as the source keeps for labels."""
    return np.ascontiguousarray(a[..., 1::2, 1::2])


def halve_max(a: np.ndarray) -> np.ndarray:
    return np.maximum(np.maximum(a[..., 0::2, 0::2], a[..., 0::2, 1::2]),
                      np.maximum(a[..., 1::2, 0::2], a[..., 1::2, 1::2]))


def halving_for(label: str | None):
    if label is None:
        return halve_mean
    return halve_max if label == MISSING else halve_pick


def offset_um(level: int, label: str | None) -> float:
    """Where a level's first pixel center lies from level 0's, along y and x.

    A block mean or maximum stands for the block's center; a kept label
    pixel is the block's bottom-right pixel.
    """
    blocks = 2**level - 1
    return blocks * PIXEL_UM if label not in (None, MISSING) else blocks / 2 * PIXEL_UM


# Every node is set up on the main thread before any pixel is written, so the
# tasks only write chunks. The coarse arrays are created again at the same
# paths with their exact shapes, which drops the copied levels' chunks from
# this snapshot; ``source-copy`` keeps them.


def retranslated(attrs: dict, label: str | None, method: str) -> dict:
    """``attrs`` with levels 1 to 3 placed by :func:`offset_um`, their method recorded."""
    multiscale = attrs["ome"]["multiscales"][0]
    for level, dataset in enumerate(multiscale["datasets"]):
        if level == 0:
            continue
        for step in dataset["coordinateTransformations"][0]["transformations"]:
            if step["type"] == "translation":
                step["translation"][-2:] = [round(offset_um(level, label), 9)] * 2
    multiscale["type"] = {None: "mean", MISSING: "max"}.get(label, "pick")
    multiscale["metadata"] = {"description": method}
    return attrs


def recreate(group: zarr.Group, level0: zarr.Array, level: int) -> zarr.Array:
    """An empty level ``level`` of the shape level 0 halves to, encoded like level 0.

    Image level 1 is sharded every 32 planes, like level 0; image levels 2 and
    3, and every label level, take all planes in one shard.
    """
    f = 2**level
    shape = (*level0.shape[:-2], level0.shape[-2] // f, level0.shape[-1] // f)
    depth_axis = len(shape) - 3
    shard = list(shape)
    shard[:depth_axis] = [1] * depth_axis
    if level == 1 and depth_axis:
        shard[depth_axis] = SHARD_PLANES
    chunks = [*([1] * (len(shape) - 2)), *shape[-2:]]
    return group.create_array(
        str(level), shape=shape, dtype=level0.dtype, fill_value=0, chunks=tuple(chunks),
        shards=tuple(shard), compressors=level0.compressors,
        dimension_names=level0.metadata.dimension_names, overwrite=True,
    )


def prepare(root: zarr.Group, path: str) -> tuple[list[str], list[tuple]]:
    """Set up one image's new levels and metadata; return its replaced arrays and its tasks."""
    image = root[path]
    attrs = retranslated(dict(image.attrs), None, IMAGE_METHOD)
    attrs["annotations"]["coarse_levels"] = (
        f"Rebuilt from level 0. The source's own coarse pixels are at tag {PARENT_TAG}.")
    validate(attrs["ome"], "0.6", model="image")
    image.attrs.put(attrs)
    replaced, tasks = [], []
    for level in range(1, LEVELS):
        recreate(image, image["0"], level)
        replaced.append(f"{path}/{level}")
    tasks += [(path, None, c) for c in range(image["0"].shape[0])]

    for name in image["labels"].attrs["ome"]["labels"]:
        group = image[f"labels/{name}"]
        method = MISSING_METHOD if name == MISSING else LABEL_METHOD
        attrs = retranslated(dict(group.attrs), name, method)
        if name == MISSING:
            label = attrs["ome"]["image-label"]
            kept = [p for p in label.get("properties", []) if p["level"] == 0]
            if kept:
                label["properties"] = kept
            else:
                label.pop("properties", None)
            attrs["annotations"]["levels"] = (
                "Level 0 marks the voxels that damaged level-0 source chunks set to 0; each "
                "coarser pixel marks a block containing any of them.")
        validate(attrs["ome"], "0.6", model="label")
        group.attrs.put(attrs)
        for level in range(1, LEVELS):
            recreate(group, group["0"], level)
            replaced.append(f"{path}/labels/{name}/{level}")
        tasks.append((path, name, None))
    return replaced, tasks



class Tally:
    """What the tasks have done so far, for the progress lines and the stall watch."""

    def __init__(self, tasks: int) -> None:
        self.tasks, self.done, self.read_bytes = tasks, 0, 0
        self.started = time.monotonic()
        self.lock = threading.Lock()

    def add(self, read_bytes: int = 0, done: int = 0) -> None:
        with self.lock:
            self.read_bytes += read_bytes
            self.done += done

    def line(self) -> str:
        elapsed = time.monotonic() - self.started
        rate = self.read_bytes / elapsed / 1e9 if elapsed else 0.0
        left = (self.tasks - self.done) * elapsed / self.done if self.done else float("nan")
        return (f"rebuilding: {self.done} of {self.tasks} tasks, {self.read_bytes / 1e9:.0f} GB of "
                f"level 0 decoded, {rate:.2f} GB/s, about {left / 60:.0f} min left")

    def signature(self) -> tuple[int, int]:
        return self.done, self.read_bytes


def rebuild(root: zarr.Group, path: str, label: str | None, channel: int | None, tally: Tally) -> None:
    """Rebuild levels 1 to 3 of one channel of an image, or of one label, from its level 0."""
    group = root[path] if label is None else root[f"{path}/labels/{label}"]
    lead = (channel,) if label is None else ()
    source = group["0"]
    halve = halving_for(label)
    depth = source.shape[-3]
    targets = [group[str(k)] for k in range(1, LEVELS)]
    # Image level 1 is written as each 32-plane shard is complete; every other
    # level is one shard over all planes, so it waits for the last plane.
    streamed = targets[:1] if label is None else []
    held = targets[len(streamed):]
    buffers = [np.zeros(t.shape[-3:], dtype=source.dtype) for t in held]
    for z0 in range(0, depth, SHARD_PLANES):
        z1 = min(z0 + SHARD_PLANES, depth)
        slab = source[(*lead, slice(z0, z1))]
        tally.add(read_bytes=slab.nbytes)
        coarser = slab
        for k, target in enumerate(targets):
            coarser = halve(coarser)
            if k < len(streamed):
                target[(*lead, slice(z0, z1))] = coarser
            else:
                buffers[k - len(streamed)][z0:z1] = coarser
    for target, buffer in zip(held, buffers, strict=True):
        target[lead] = buffer
    tally.add(done=1)



def aligned(root: zarr.Group, path: str) -> bool:
    """Whether the field starts on a level-3 block of the well image, so every level lines up."""
    roi = root[path].attrs["annotations"]["FOV_ROI_table"]
    return all(round(roi[f"{a}_micrometer"] / PIXEL_UM) % 2 ** (LEVELS - 1) == 0 for a in "yx")


def check_against_source(root: zarr.Group, before: zarr.Group, source: zarr.Group, path: str) -> str:
    """Every rebuilt level of an aligned image equals the source's, channel by channel and label by label.

    Damaged source chunks are left out. A lost level-0 chunk reads as 0, so
    the rebuild averages zeros where the source, which built its levels
    before the loss, did not; a lost coarse chunk zeroes the source's level
    but not the rebuilt one. ``missing_source`` marks both: before the
    rebuild (``before``) it marks each level's own damage, and after it, the
    level-0 damage carried up. Voxels either marks are skipped, for every
    channel and label, which skips some good voxels and no damaged ones.
    """
    compared = skipped = 0
    names = [n for n in root[f"{path}/labels"].attrs["ome"]["labels"] if n != MISSING]
    for level in range(1, LEVELS):
        ours, theirs = root[f"{path}/{level}"], source[f"{path}/{level}"]
        if ours.shape != theirs.shape:
            raise SystemExit(f"{path} level {level}: shape {ours.shape}, the source's cut is {theirs.shape}")
        good = ((before[f"{path}/labels/{MISSING}/{level}"][...] == 0)
                & (root[f"{path}/labels/{MISSING}/{level}"][...] == 0))
        skipped += int((~good).sum())
        pairs = [(f"channel {c}", ours[c], theirs[c]) for c in range(ours.shape[0])]
        pairs += [(name, root[f"{path}/labels/{name}/{level}"][...], source[f"{path}/labels/{name}/{level}"][...])
                  for name in names]
        for what, a, b in pairs:
            if not np.array_equal(a[good], b[good]):
                raise SystemExit(f"{path} {what} level {level}: {int((a[good] != b[good]).sum())} "
                                 f"voxels differ from {PARENT_TAG} outside the damaged chunks")
            compared += 1
    return (f"{path}: {compared} channel and label levels equal {PARENT_TAG}, "
            f"leaving out {skipped:,} voxels in damaged chunks over the three levels")


def check_recomputed(root: zarr.Group, path: str, rng: random.Random) -> None:
    """One channel and one label at one plane, rebuilt again from level 0, equal what was written."""
    image = root[path]
    channel, plane = rng.randrange(image["0"].shape[0]), rng.randrange(image["0"].shape[1])
    name = rng.choice(image["labels"].attrs["ome"]["labels"])
    for label, group, lead in ((None, image, (channel, plane)), (name, image[f"labels/{name}"], (plane,))):
        expected = group["0"][lead]
        halve = halving_for(label)
        for level in range(1, LEVELS):
            expected = halve(expected)
            if not np.array_equal(group[str(level)][lead], expected):
                raise SystemExit(f"{path} {label or f'channel {channel}'} plane {plane} level {level} "
                                 "differs from a fresh rebuild")


def check_metadata(root: zarr.Group, path: str) -> None:
    groups = [(None, root[path])]
    groups += [(n, root[f"{path}/labels/{n}"]) for n in root[f"{path}/labels"].attrs["ome"]["labels"]]
    for label, group in groups:
        attrs = dict(group.attrs)
        validate(attrs["ome"], "0.6", model="image" if label is None else "label")
        datasets = attrs["ome"]["multiscales"][0]["datasets"]
        for level, dataset in enumerate(datasets):
            steps = {t["type"]: t for t in dataset["coordinateTransformations"][0]["transformations"]}
            want = offset_um(level, label) if level else 0.0
            if not np.allclose(steps["translation"]["translation"][-2:], want, atol=1e-9):
                raise SystemExit(f"{path} {label or 'image'} level {level}: translation "
                                 f"{steps['translation']['translation']}, expected y and x {want}")
            if group[str(level)].shape[-2:] != (1000 // 2**level,) * 2:
                raise SystemExit(f"{path} {label or 'image'} level {level}: shape {group[str(level)].shape}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="check everything, commit nothing")
    parser.add_argument("--images", help="comma-separated well/field paths to rebuild; implies --dry-run")
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    dry_run = args.dry_run or bool(args.images)

    zarr.config.set({"async.concurrency": 4 * args.workers, "threading.max_workers": args.workers})
    here = Path(__file__)
    code = None if dry_run else code_ref(here, here.with_name("change.py"))
    repo = arraylake_repository(REPO)
    session = enhance_session(repo)
    root = zarr.open_group(session.store, mode="r+")
    source = zarr.open_group(repo.readonly_session(tag=PARENT_TAG).store, mode="r")
    before = zarr.open_group(repo.readonly_session(snapshot_id=session.snapshot_id).store, mode="r")

    paths = args.images.split(",") if args.images else image_paths(root)
    replaced, tasks = [], []
    for path in paths:
        r, t = prepare(root, path)
        replaced += r
        tasks += t
    print(f"set up {len(replaced)} arrays in {len(paths)} images; {len(tasks)} tasks", flush=True)

    tally = Tally(len(tasks))
    # Large tasks first, so the pool does not end on one long label.
    tasks.sort(key=lambda t: t[1] is None)
    with Heartbeat(tally.line), StallWatch(tally.signature, after=120.0), \
            ThreadPoolExecutor(args.workers) as pool:
        for future in [pool.submit(rebuild, root, *task, tally) for task in tasks]:
            future.result()
    print(tally.line(), flush=True)

    reread = zarr.open_group(session.store, mode="r")
    rng = random.Random(19)
    lines = []
    for n, path in enumerate(paths, start=1):
        check_metadata(reread, path)
        check_recomputed(reread, path, rng)
        if aligned(reread, path):
            lines.append(check_against_source(reread, before, source, path))
        if n % 20 == 0:
            print(f"checked {n} of {len(paths)} images", flush=True)
    labels_groups = {f"{p}/labels/{n}" for p in paths for n in root[f"{p}/labels"].attrs["ome"]["labels"]}
    check_session(session, updated_groups=set(paths) | labels_groups,
                  replaced_arrays=set(replaced), chunks_under=set(replaced))
    shards = sum(len(v) for v in session.status().updated_chunks.values())
    print("\n".join(lines))

    checked = (
        f"Read back before committing: on the {len(lines)} images whose origin falls on a "
        "level-3 block boundary, every rebuilt level of every channel and label equals the "
        f"source's at tag {PARENT_TAG} outside the voxels missing_source marks; on every image, one channel and one label at one plane "
        "rebuilt again from level 0 equal what was written; every image and label has levels of "
        "1000, 500, 250 and 125 pixels, the translations stated, and metadata valid under "
        f"OME-Zarr 0.6; the session replaced exactly the {len(replaced)} coarse arrays."
    )
    print(checked)
    print(f"{shards} shards written")
    if dry_run:
        print("dry run: nothing committed")
        return

    message = (
        "Rebuild every image's levels 1 to 3 from its own level 0\n\n"
        "The copied levels were the source's coarse pixels of the whole well image, cut to "
        "each field, so most were a pixel wider than 1000 / 2^k, averaged the field's edge "
        "with the empty well around it, and needed a translation per field. Each level is "
        "now built from the field's level 0 the way the source builds its own: 2 x 2 means of "
        "the level above, rounded half up. Labels keep the bottom-right pixel of each block, "
        "and missing_source the block's largest value. Every image's levels are exactly "
        "500, 250 and 125 pixels with one translation per level. Levels 2 and 3 are stored "
        "as one shard per channel over all planes. The source's coarse pixels remain at "
        f"tag {PARENT_TAG}.\n\n" + checked
    )
    metadata = change(
        summary="Levels 1 to 3 of every image and label are rebuilt from level 0, 500, 250 and 125 pixels wide.",
        reason="The copied levels were cut from well-wide pyramids: uneven widths, edges "
        "averaged with empty well, and a translation per field.",
        kind="pixels",
        scope={"pattern": "*/*/*/{1,2,3} and */*/*/labels/*/{1,2,3}", "count": len(replaced)},
        chunks_written=shards,
        details={
            "image_method": IMAGE_METHOD, "label_method": LABEL_METHOD, "missing_source_method": MISSING_METHOD,
            "translation_um_by_level": {"image": {str(k): offset_um(k, None) for k in range(LEVELS)},
                                        "label": {str(k): offset_um(k, "cells") for k in range(LEVELS)}},
            "shard_planes_by_level": {"0": SHARD_PLANES, "1": SHARD_PLANES, "2": "all", "3": "all"},
        },
        checked=checked,
        code=code,
    )
    snapshot = commit(session, message, metadata=metadata)
    print(f"committed {snapshot} on enhance")


if __name__ == "__main__":
    main()
