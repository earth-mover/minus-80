"""Spot-check a CAMELYON store against the bucket it references.

``test_ingest.py`` checks the ingest's code on a small fixture, where every
tile is cheap to compare. This script checks what depends on the real
archive, in about a minute and a few hundred range requests:

- ``complete``: every slide asked for is in the store, with a mask exactly
  where the bucket has one and outlines exactly where it has an XML, and its
  row agrees with the challenge's label table. No TIFF is read.
- ``outlines``: each outline table rebuilds its ASAP XML byte for byte, with
  the MD5 that ``checksums.md5`` lists, and its GeoJSON agrees with the
  mask's tumor pixels away from the outlines' edges.
- ``pixels``: on a few slides, one of each TIFF dialect where the sample has
  it, the middle tile of level 0, the whole coarsest level and the middle
  stored mask tile read through the store equal tifffile's decode of the
  same region; derived mask levels have the image's shape and only values
  the mask file has.
- the OME-Zarr gates of ``minus80.ome.verify``.

    uv run --package camelyon-ingest python ingests/camelyon/verify.py \\
        --repo <dir | arraylake://org/repo> --challenge CAMELYON17 [--slides a,b] [--pixels 3]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time

import ingest
import numpy as np
import shapely
import zarr
from ingest import C16, C17, CHALLENGES, CONTEXTS, slide_path
from minus80.core.documents import read_text
from minus80.ome.verify import GATES, run_gates
from minus80.parsers import asap
from plumbing import SOURCE, Source, open_repository


def complete(root, challenge, slides, ctx) -> list[str]:
    problems = []
    table = dict(root[challenge.name].attrs)["annotations"]
    in_table = set(table["slide"])
    for slide in slides:
        path = slide_path(challenge, slide)
        if path not in root or "annotations" not in root[path].attrs:
            problems.append(f"{slide}: not in the store")
            continue
        row = dict(root[path].attrs)["annotations"]
        group = root[path]
        has_mask = "labels" in group["image"]
        has_shapes = "shapes" in group
        if challenge is C16:
            want_mask, want_shapes = True, f"annotations/{slide}.xml" in ctx["sums"]
            labels = {k: ctx["reference"][slide][k] for k in ("type", "class", "center")}
        else:
            want_mask, want_shapes = slide in ctx["masks"], slide in ctx["annotations"]
            patient = ctx["patients"][slide.rsplit("_node_", 1)[0]]
            labels = {"stage": ctx["labels"].get(slide), "center": patient["center"], "split": patient["split"]}
        if (has_mask, has_shapes) != (want_mask, want_shapes):
            problems.append(f"{slide}: mask {has_mask}, shapes {has_shapes}; the bucket has {want_mask}, {want_shapes}")
        if {k: row[k] for k in labels} != labels:
            problems.append(f"{slide}: row {({k: row[k] for k in labels})}, label table {labels}")
        if slide not in in_table:
            problems.append(f"{slide}: missing from the container's table")
    print(f"  {len(slides)} slides: groups, masks, outlines and labels "
          f"{'as the bucket lists them' if not problems else 'DIFFER'}; the table holds {len(in_table)} slides")
    return problems


#: The share of tumor pixels on which the GeoJSON may disagree with the
#: reference mask away from the outlines' edges. The challenge made the masks
#: from these polygons, so the two differ only along edges, where a coarse
#: level rounds either way; pixels within EDGE_PX of an outline are not
#: counted. A missing hole, a lost shape or a wrong frame shows up as
#: disagreement away from the edges.
MAX_INTERIOR_DISAGREEMENT = 0.001
EDGE_PX = 1.5


def geojson_against_mask(group) -> tuple[str, bool]:
    """Rasterize ``shapes/geojson`` on a coarse level of the slide's mask and compare tumor pixels."""
    fc = json.loads(bytes(group["shapes/geojson"][:]))
    tumor = shapely.union_all([shapely.geometry.shape(f["geometry"]) for f in fc["features"]
                               if f["properties"].get("classification", {}).get("name") == "Tumor"])
    if tumor.is_empty:
        return "no Tumor area", True
    x0, y0, x1, y1 = tumor.bounds
    level = 3
    while (x1 - x0) * (y1 - y0) / 4 ** level > 4e6:
        level += 1
    f = 2 ** level
    ys, xs = max(int(y0 // f) - 2, 0), max(int(x0 // f) - 2, 0)
    want = group[f"image/labels/mask/{level}"][ys:int(y1 // f) + 3, xs:int(x1 // f) + 3] == 2
    yy, xx = np.mgrid[ys:ys + want.shape[0], xs:xs + want.shape[1]]
    cx, cy = (xx + 0.5) * f, (yy + 0.5) * f
    wrong = shapely.contains_xy(tumor, cx, cy) ^ want
    edge = shapely.contains_xy(tumor.boundary.buffer(EDGE_PX * f), cx, cy)
    interior = int((wrong & ~edge).sum())
    ok = interior / max(int(want.sum()), 1) <= MAX_INTERIOR_DISAGREEMENT
    return (f"geojson {len(fc['features'])} features on mask level {level}: {interior} of {int(want.sum())} "
            f"tumor pixels disagree away from the edges"), ok


def outlines(root, challenge, slides, ctx, source: Source) -> list[str]:
    problems = []
    for slide in slides:
        path = f"{slide_path(challenge, slide)}/shapes"
        if path not in root:
            continue
        g = root[path]
        table = {k: g[k][:] for k in ("coords", "offset0", *asap.COLUMNS)}
        table["groups"] = dict(g.attrs)["asap_groups"]
        xml = asap.to_xml(table)
        key = f"annotations/{slide}.xml"
        same = xml == source.read(challenge.prefix + key) and hashlib.md5(xml).hexdigest() == ctx["sums"][key]
        line, ok = geojson_against_mask(root[slide_path(challenge, slide)])
        print(f"  {slide}: XML {'rebuilt byte for byte, MD5 as listed' if same else 'DIFFERS'}; {line}")
        if not same:
            problems.append(f"{slide}: the outline table does not rebuild the XML")
        if not ok:
            problems.append(f"{slide}: {line}")
    return problems


def same_region(stored: zarr.Array, reference: zarr.Array, ys: slice, xs: slice) -> bool:
    got = stored[:, ys, xs] if stored.ndim == 3 else stored[ys, xs]
    want = reference[ys, xs]
    return np.array_equal(got, np.moveaxis(want, -1, 0) if stored.ndim == 3 else want)


def tifffile_level(level) -> zarr.Array:
    ref = zarr.open(level.aszarr(), mode="r")
    return ref["0"] if isinstance(ref, zarr.Group) else ref


def pixel_sample(root, challenge, slides, n: int) -> list[str]:
    """Up to ``n`` slides, taking one of each TIFF dialect first."""
    by_kind: dict[str, list[str]] = {}
    for slide in slides:
        kind = "philips" if "scanned_um" in dict(root[f"{slide_path(challenge, slide)}/image"].attrs)["pixel_size"] else "generic"
        by_kind.setdefault(kind, []).append(slide)
    picked = [group[0] for group in by_kind.values()]
    rest = [s for s in slides if s not in picked]
    return (picked + rest)[:n]


def pixels(root, challenge, slides, source: Source) -> list[str]:
    problems = []
    for slide in slides:
        path = slide_path(challenge, slide)
        tf = source.open_tiff(f"{challenge.prefix}images/{slide}.tif")
        levels = tf.series[0].levels
        image = root[f"{path}/image"]
        top, bottom = image["0"], image[str(len(levels) - 1)]
        th, tw = top.chunks[1:]
        my, mx = top.shape[1] // 2 // th * th, top.shape[2] // 2 // tw * tw
        checks = {
            "level 0 middle tile": same_region(top, tifffile_level(levels[0]), slice(my, my + th), slice(mx, mx + tw)),
            f"level {len(levels) - 1} whole": same_region(bottom, tifffile_level(levels[-1]), slice(None), slice(None)),
        }
        if "labels" in image:
            mask = image["labels/mask"]
            mtf = source.open_tiff(f"{challenge.prefix}masks/{slide}_mask.tif")
            page = mtf.series[0].levels[0].keyframe
            mh, mw = mask["0"].chunks
            grid = (-(-mask["0"].shape[0] // mh), -(-mask["0"].shape[1] // mw))
            stored = np.argwhere(np.asarray(page.databytecounts).reshape(grid) > 0)
            ty, tx = stored[len(stored) // 2]
            checks["mask level 0 stored tile"] = same_region(
                mask["0"], tifffile_level(mtf.series[0].levels[0]),
                slice(ty * mh, ty * mh + mh), slice(tx * mw, tx * mw + mw))
            values = set(np.unique(mtf.series[0].levels[-1].asarray()).tolist())
            for k in dict(mask.attrs).get("derived_levels", {}):
                derived = set(np.unique(mask[k][:]).tolist())
                checks[f"derived mask level {k}"] = (derived <= values and
                                                     mask[k].shape[0] >= image[k].shape[1] and
                                                     mask[k].shape[1] >= image[k].shape[2])
        bad = [name for name, ok in checks.items() if not ok]
        print(f"  {slide}: {', '.join(checks)}: {'identical to tifffile' if not bad else 'DIFFER: ' + ', '.join(bad)}")
        problems += [f"{slide}: {name}" for name in bad]
    return problems


def source_files(root, challenge, source: Source) -> list[str]:
    attrs = dict(root[challenge.name].attrs)["source_files"]
    bad = [key for key, value in attrs.items()
           if read_text(root, value).encode() != source.read(challenge.prefix + key)]
    print(f"  {len(attrs)} source files: {'identical' if not bad else 'DIFFER: ' + ', '.join(bad)}")
    return bad


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--challenge", choices=sorted(CHALLENGES), required=True)
    ap.add_argument("--slides", help="comma-separated slides to check; default every slide in the store")
    ap.add_argument("--pixels", type=int, default=3, help="slides whose tiles are compared with tifffile")
    ap.add_argument("--source", default=SOURCE)
    args = ap.parse_args(argv)
    started = time.monotonic()
    challenge = CHALLENGES[args.challenge]
    source = Source(args.source)
    root = zarr.open_group(open_repository(args.repo, source).readonly_session("main").store, mode="r")
    ctx = CONTEXTS[challenge.name](source)
    slides = args.slides.split(",") if args.slides else list(dict(root[challenge.name].attrs)["annotations"]["slide"])
    print(f"checking {len(slides)} {challenge.name} slides in {args.repo}")
    failed = 0
    for label, check in [
        ("complete", lambda: complete(root, challenge, slides, ctx)),
        ("source files", lambda: source_files(root, challenge, source)),
        ("outlines", lambda: outlines(root, challenge, slides, ctx, source)),
        ("pixels", lambda: pixels(root, challenge, pixel_sample(root, challenge, slides, args.pixels), source)),
    ]:
        print(label)
        problems = check()
        for problem in problems[:10]:
            print(f"  FAIL {problem}")
        failed += bool(problems)
    print("OME-Zarr gates")
    failed += run_gates(root[challenge.name], dict(GATES), {"version": challenge.version, "sample": 10})
    print(f"{'FAILED' if failed else 'all checks pass'} in {time.monotonic() - started:.0f} s")
    sys.exit(bool(failed))


if __name__ == "__main__":
    main()
