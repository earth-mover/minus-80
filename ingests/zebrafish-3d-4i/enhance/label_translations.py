"""Place each coarse label level on the pixel it keeps (PLAN.md decision 17).

Why the labels are off
----------------------
The source builds its coarse levels two ways. Image level k averages each
2^k × 2^k block of level 0, so coarse pixel ``j`` stands for the block's
center. Label level k cannot average (the mean of two label values is a third,
unrelated label), so it keeps one pixel of each block: the bottom-right one.
On B/02/0 and E/04/3, ``cells`` levels 1 to 3 equal that pixel exactly on
every labeled pixel of plane 135, and no other pixel of the block matches.

The copy gave each label level its image's translation, which is right for a
block center. The pixel a label keeps lies (2^k − 1)/2 level-0 pixels further
down and right, so this script adds that much to each label level's y and x
translation: 0.325 µm at level 1, 0.975 µm at level 2 and 2.275 µm at level 3,
where 2.275 µm is about half a level-3 pixel.

What it touches
---------------
Only the ``multiscales`` of each label group changes; every chunk stays the
source's. Before writing, the script checks that every label level still has
its image's translation, so running it a second time stops instead of shifting
the labels twice. After writing, and before committing, it reads back:

- every label level's translation, which must be its image's plus the shift;
- the session's changes, which must be exactly the label groups, with no array
  or chunk among them;
- ``cells`` plane 135 of B/02/0 and E/04/3, where each coarse label pixel,
  placed by the new translation, must equal the level-0 label pixel there.

Run ``--dry-run`` to do all of that without committing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import zarr

sys.path.insert(0, str(Path(__file__).parent))
from change import REPO, change, code_ref, enhance_session  # noqa: E402

from minus80.core.icechunk_io import arraylake_repository, commit  # noqa: E402

PIXEL_UM = 0.65
LEVELS = 4
SAMPLE = (("B/02/0", "cells", 135), ("E/04/3", "cells", 135))


def shift_um(level: int) -> float:
    """How far a level's kept pixel lies below and right of its block's center."""
    return (2**level - 1) / 2 * PIXEL_UM


def _transforms(attrs: dict) -> list[tuple[list[float], list[float]]]:
    """(scale, translation) of each dataset of a multiscales, level 0 first."""
    out = []
    for dataset in attrs["ome"]["multiscales"][0]["datasets"]:
        (sequence,) = dataset["coordinateTransformations"]
        steps = {t["type"]: t for t in sequence["transformations"]}
        out.append((steps["scale"]["scale"], steps["translation"]["translation"]))
    return out


def label_paths(root: zarr.Group) -> list[tuple[str, str]]:
    """Every (image path, label path) the plate lists, in plate order."""
    pairs = []
    for well in root.attrs["ome"]["plate"]["wells"]:
        for image in root[well["path"]].attrs["ome"]["well"]["images"]:
            image_path = f"{well['path']}/{image['path']}"
            if "labels" not in root[image_path]:
                continue
            for name in root[f"{image_path}/labels"].attrs["ome"]["labels"]:
                pairs.append((image_path, f"{image_path}/labels/{name}"))
    return pairs


def shifted(label_attrs: dict, image_attrs: dict, where: str) -> dict:
    """``label_attrs`` with each level's y and x translation moved onto the kept pixel."""
    image_yx = [t[-2:] for _, t in _transforms(image_attrs)]
    label_yx = [t[-2:] for _, t in _transforms(label_attrs)]
    if len(label_yx) != LEVELS or not np.allclose(label_yx, image_yx, atol=1e-9):
        raise SystemExit(
            f"{where}: label translations {label_yx} are not the image's {image_yx}; "
            "already shifted, or not the layout this script expects"
        )
    ome = label_attrs["ome"]
    for level, dataset in enumerate(ome["multiscales"][0]["datasets"]):
        for step in dataset["coordinateTransformations"][0]["transformations"]:
            if step["type"] == "translation":
                t = step["translation"]
                t[-2:] = [round(v + shift_um(level), 9) for v in t[-2:]]
    return {"ome": ome}


def check_translations(root: zarr.Group, pairs: list[tuple[str, str]]) -> None:
    for image_path, label_path in pairs:
        image = _transforms(dict(root[image_path].attrs))
        label = _transforms(dict(root[label_path].attrs))
        for level, ((_, ti), (_, tl)) in enumerate(zip(image, label, strict=True)):
            moved = np.subtract(tl[-2:], ti[-2:])
            if not np.allclose(moved, shift_um(level), atol=1e-9):
                raise SystemExit(f"{label_path} level {level}: moved {moved}, not {shift_um(level)}")


def check_changes(session, pairs: list[tuple[str, str]]) -> None:
    diff = session.status()
    expected = {f"/{label}" for _, label in pairs}
    if set(diff.updated_groups) != expected:
        raise SystemExit(
            f"updated groups differ from the labels: "
            f"{sorted(set(diff.updated_groups) ^ expected)[:10]}"
        )
    others = (diff.new_groups, diff.new_arrays, diff.deleted_groups, diff.deleted_arrays,
              diff.updated_arrays, diff.updated_chunks, diff.moved_nodes)
    if any(others):
        raise SystemExit(f"the session changed more than label metadata: {diff}")


def check_pixels(root: zarr.Group) -> list[str]:
    """Each coarse label pixel, placed by its translation, equals the level-0 pixel there."""
    lines = []
    for image_path, name, plane in SAMPLE:
        label = root[f"{image_path}/labels/{name}"]
        transforms = _transforms(dict(label.attrs))
        full = label["0"][plane]
        (_, t0) = transforms[0]
        for level in range(1, LEVELS):
            scale, t = transforms[level]
            coarse = label[str(level)][plane]
            # The level-0 index of coarse pixel j along an axis.
            index = [
                (np.arange(n) * scale[a] + t[a] - t0[a]) / PIXEL_UM
                for a, n in ((1, coarse.shape[0]), (2, coarse.shape[1]))
            ]
            if not all(np.allclose(i, np.round(i), atol=1e-6) for i in index):
                raise SystemExit(f"{image_path} {name} level {level}: pixels fall between level-0 pixels")
            ys, xs = (np.round(i).astype(int) for i in index)
            vy, vx = (ys >= 0) & (ys < full.shape[0]), (xs >= 0) & (xs < full.shape[1])
            a = coarse[np.ix_(vy, vx)]
            b = full[np.ix_(ys[vy], xs[vx])]
            labeled = (a > 0) | (b > 0)
            match = float((a[labeled] == b[labeled]).mean())
            if match != 1.0:
                raise SystemExit(f"{image_path} {name} level {level}: {match:.4%} of labeled pixels match")
            lines.append(f"{image_path} {name} plane {plane} level {level}: "
                         f"{int(labeled.sum())} labeled pixels, all equal")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="check everything, commit nothing")
    args = parser.parse_args()

    code = None if args.dry_run else code_ref(Path(__file__), Path(__file__).with_name("change.py"))
    repo = arraylake_repository(REPO)
    session = enhance_session(repo)
    root = zarr.open_group(session.store, mode="r+")

    pairs = label_paths(root)
    print(f"{len(pairs)} label images under {len({i for i, _ in pairs})} images")
    for image_path, label_path in pairs:
        root[label_path].attrs.update(
            shifted(dict(root[label_path].attrs), dict(root[image_path].attrs), label_path)
        )

    reread = zarr.open_group(session.store, mode="r")
    check_translations(reread, pairs)
    check_changes(session, pairs)
    pixel_lines = check_pixels(reread)
    print(f"translations: all {len(pairs)} label images moved by the shift at every level")
    print(f"changes: {len(pairs)} label groups updated, no arrays or chunks")
    print("\n".join(pixel_lines))

    shifts = {str(k): shift_um(k) for k in range(LEVELS)}
    checked = (
        f"Read back before committing: every label level's y and x translation equals its "
        f"image's plus the shift, in all {len(pairs)} label images; the session changed "
        f"exactly those {len(pairs)} groups and no array or chunk; on "
        + " and ".join(f"{i} {n} plane {p}" for i, n, p in SAMPLE)
        + ", every coarse label pixel at levels 1 to 3, placed by its new translation, "
        "equals the level-0 label pixel there."
    )
    if args.dry_run:
        print("dry run: nothing committed")
        return

    message = (
        "Place each coarse label level on the pixel it keeps\n\n"
        "The source's label levels 1 to 3 keep the bottom-right pixel of each 2^k block, "
        "but the copy gave them their image's translations, which place a coarse pixel at "
        "the block's center. This adds (2^k - 1)/2 x 0.65 um to each label level's y and x "
        "translation: 0.325, 0.975 and 2.275 um at levels 1, 2 and 3. Only the label "
        "groups' metadata changes; every label pixel is still the source's (PLAN.md "
        f"decision 17).\n\n{checked}"
    )
    metadata = change(
        plan_decision=17,
        summary="Each label level's y and x translation moves onto the pixel the level keeps.",
        reason="Label levels keep the bottom-right pixel of each block but carried the "
        "image's block-center translations, which placed level 3 about half a pixel off.",
        kind="metadata",
        scope={"pattern": "*/*/*/labels/*", "count": len(pairs)},
        chunks_written=0,
        details={"shift_um_by_level": shifts, "axes": ["y", "x"]},
        checked=checked,
        code=code,
    )
    snapshot = commit(session, message, metadata=metadata)
    print(f"committed {snapshot} on enhance")


if __name__ == "__main__":
    main()
