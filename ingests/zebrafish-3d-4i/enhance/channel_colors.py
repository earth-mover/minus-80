"""Give the four channels of each round four distinct colors (PLAN.md decision 16).

Why the source's colors are replaced
------------------------------------
The source gives each channel an ``omero`` color, and within a round they
repeat. Every image of the plate has the same pattern: round 2 colors DAPI,
H2B and ALYREF all ``0000FF``, and round 0 shows FLAG and PCNA in the same
blue as DAPI. A viewer that applies these colors draws three of a round's
four stains as one shade of blue.

This script colors each round the same way: DAPI blue, then the round's
other three channels green, magenta and yellow, in the source's channel
order. Any one round then displays with four colors that are easy to tell
apart. The source's color is kept in each channel's ``annotations.channels``
entry as ``source_color``, so nothing the source said is lost. Display
windows, labels and every other ``omero`` field stay as they are.

What it touches
---------------
Only the attributes of the 211 images change; no array or chunk is written.
Before writing, the script checks that each image's first channel of every
round is a DAPI channel and that no ``source_color`` is recorded yet, so a
second run stops instead of overwriting the kept colors with new ones.
After writing, and before committing, it reads back every image's colors and
``source_color`` values and the session's list of changes.

Run ``--dry-run`` to do all of that without committing.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import zarr

sys.path.insert(0, str(Path(__file__).parent))
from change import REPO, change, check_session, code_ref, enhance_session, image_paths  # noqa: E402

from minus80.core.icechunk_io import arraylake_repository, commit  # noqa: E402

#: Blue for DNA, then green, magenta and yellow, by position within the round.
ROUND_COLORS = ("0000FF", "00FF00", "FF00FF", "FFFF00")


def recolored(attrs: dict, where: str) -> dict:
    """``attrs`` with each round's channels colored :data:`ROUND_COLORS`, the source's kept."""
    channels = attrs["ome"]["omero"]["channels"]
    notes = attrs["annotations"]["channels"]
    if len(channels) != len(notes):
        raise SystemExit(f"{where}: {len(channels)} omero channels, {len(notes)} annotated")
    position = Counter()
    for channel, note in zip(channels, notes, strict=True):
        if "source_color" in note:
            raise SystemExit(f"{where}: {note['label']} already has a source_color")
        if channel["label"] != note["label"]:
            raise SystemExit(f"{where}: omero says {channel['label']}, annotations {note['label']}")
        index = position[note["round"]]
        position[note["round"]] += 1
        if index == 0 and not note["label"].startswith("DAPI"):
            raise SystemExit(f"{where}: round {note['round']} starts with {note['label']}, not DAPI")
        note["source_color"] = channel["color"]
        channel["color"] = ROUND_COLORS[index]
    return attrs


def check_colors(root: zarr.Group, paths: list[str], source: dict[str, list[str]]) -> None:
    for path in paths:
        attrs = dict(root[path].attrs)
        notes = attrs["annotations"]["channels"]
        colors = [c["color"] for c in attrs["ome"]["omero"]["channels"]]
        kept = [n["source_color"] for n in notes]
        if kept != source[path]:
            raise SystemExit(f"{path}: source_color {kept} is not the source's {source[path]}")
        for round_ in {n["round"] for n in notes}:
            in_round = [c for c, n in zip(colors, notes) if n["round"] == round_]
            if tuple(in_round) != ROUND_COLORS:
                raise SystemExit(f"{path} round {round_}: colors {in_round}")


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
    source = {}
    for path in paths:
        attrs = dict(root[path].attrs)
        source[path] = [c["color"] for c in attrs["ome"]["omero"]["channels"]]
        root[path].attrs.put(recolored(attrs, path))
    print(f"recolored {len(paths)} images")

    check_colors(zarr.open_group(session.store, mode="r"), paths, source)
    check_session(session, updated_groups=set(paths))
    checked = (
        f"Read back before committing: in all {len(paths)} images every round's channels are "
        f"{', '.join(ROUND_COLORS)} in order, and every channel's source_color equals the "
        f"color the source gave it; the session changed exactly those {len(paths)} groups "
        "and no array or chunk."
    )
    print(checked)
    if args.dry_run:
        print("dry run: nothing committed")
        return

    message = (
        "Color each round's four channels blue, green, magenta and yellow\n\n"
        "The source's omero colors repeat within a round: round 2 colors DAPI, H2B and "
        "ALYREF all 0000FF, so a viewer shows three of its stains in one blue. Each round "
        "now shows DAPI blue and its other three channels green, magenta and yellow. Each "
        "channel's source color is kept as source_color in its annotations.channels entry; "
        "display windows are unchanged (PLAN.md decision 16).\n\n" + checked
    )
    metadata = change(
        plan_decision=16,
        summary="Each round's channels are colored blue (DAPI), green, magenta and yellow.",
        reason="The source's colors repeat within a round, so a viewer drew three of a "
        "round's four stains in the same blue.",
        kind="metadata",
        scope={"pattern": "*/*/*", "count": len(paths)},
        chunks_written=0,
        details={"colors_by_position_in_round": list(ROUND_COLORS), "source_kept_as": "source_color"},
        checked=checked,
        code=code,
    )
    snapshot = commit(session, message, metadata=metadata)
    print(f"committed {snapshot} on enhance")


if __name__ == "__main__":
    main()
