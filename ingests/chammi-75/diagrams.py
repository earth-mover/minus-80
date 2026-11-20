"""Draw the CHAMMI-75 ingest: the bucket, how it maps onto the store, and the store.

Every number comes from the work directory's local copies of the source (the
metadata as Parquet and the guidance zip's member list) or from a built store.

    uv run --package chammi-75-ingest python ingests/chammi-75/diagrams.py \\
        --work <ingest work dir> --repo <built store> [--out ingests/chammi-75/diagrams]

- ``source.svg``: the bucket's files, with the studies split by license.
- ``mapping.svg``: one stack of the built store: its images' PNGs, metadata
  rows and position files, and the arrays they become.
- ``store.svg``: the store's hierarchy, with each level's attributes and counts.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import zarr

from minus80 import diagrams as d

import pipeline
from ingest import CC0, CC_BY, EXCLUDED, LICENSES, SHARE_ALIKE
from verify import load_index, open_store

WIDTH = 1180


def source_counts(work: Path) -> dict:
    """Files, images and studies in the bucket, by license, from the local metadata and member list."""
    metadata = work / f"{Path(pipeline.METADATA_CSV).stem}.parquet"
    small = work / f"{Path(pipeline.SMALL_CSV).stem}.parquet"
    rows = duckdb.sql(f"""select "experiment.study" study, count(distinct "imaging.multi_channel_id") images,
                                 count(*) pngs from read_parquet('{metadata}') group by 1""").fetchall()
    by = {"included": [0, 0, 0], "share-alike": [0, 0, 0], "other": [0, 0, 0]}
    for study, images, pngs in rows:
        kind = ("included" if study in LICENSES else
                "share-alike" if EXCLUDED.get(study) == SHARE_ALIKE else "other")
        by[kind] = [by[kind][0] + 1, by[kind][1] + images, by[kind][2] + pngs]
    members = duckdb.sql(f"select count(*) from read_parquet('{work / 'guidance-members.parquet'}')").fetchall()[0][0]
    small_rows = duckdb.sql(f"select count(*) from read_parquet('{small}')").fetchall()[0][0]
    return {"by": by, "members": members, "small": small_rows,
            "pngs": sum(r[2] for r in rows), "images": sum(r[1] for r in rows), "studies": len(rows)}


def draw_source(counts: dict, out: Path) -> None:
    c = d.Canvas(WIDTH)
    y = d.heading(c, "CHAMMI-75 in the bucket", "s3://chammi-data/CHAMMI-75/  (us-west-2, anonymous)") + 30
    left, right = c.margin, 560
    nodes = [
        d.TreeNode("CHAMMI-75/", y, 0),
        d.TreeNode("CHAMMI-75_train/", y + 34, 1, count=counts["studies"], subtitle="one directory per study"),
        d.TreeNode("<study>/…/<image>_ch<slot>.png", y + 76, 2, count=counts["pngs"],
                   subtitle="8-bit grayscale, one per channel"),
        d.TreeNode(pipeline.METADATA_CSV, y + 118, 1, subtitle=f"{counts['pngs']:,} rows, one per PNG"),
        d.TreeNode(pipeline.SMALL_CSV, y + 160, 1, subtitle=f"{counts['small']:,} rows, paths under CHAMMI-75_small/"),
        d.TreeNode(pipeline.GUIDANCE_ZIP, y + 202, 1, subtitle=f"{counts['members']:,} members"),
        d.TreeNode("<study>/…/<image>_ch<slot>.safetensors", y + 244, 2,
                   subtitle="cell positions (x, y); areas in most Cellpose files"),
    ]
    bottom = d.tree(c, left, nodes)
    by = counts["by"]
    rows = [
        [d.Cell([f"{CC_BY} or {CC0}"], "added"), d.Cell([f"{by['included'][0]}"]),
         d.Cell([f"{by['included'][1]:,}"]), d.Cell([f"{by['included'][2]:,}"]), d.Cell(["stored"])],
        [d.Cell(["CC BY-SA"], "removed"), d.Cell([f"{by['share-alike'][0]}"]),
         d.Cell([f"{by['share-alike'][1]:,}"]), d.Cell([f"{by['share-alike'][2]:,}"]), d.Cell(["left out"])],
        [d.Cell(["non-commercial, custom"], "removed"), d.Cell([f"{by['other'][0]}"]),
         d.Cell([f"{by['other'][1]:,}"]), d.Cell([f"{by['other'][2]:,}"]), d.Cell(["left out"])],
    ]
    c.text(right, y + 4, "Studies by license (arXiv:2512.20833v2, Table A4)", size=13, weight="bold")
    d.table(c, right, y + 16, [190, 70, 110, 110, 90], ["license", "studies", "images", "PNGs", "store"], rows)
    c.bottom = max(c.bottom, bottom)
    c.write(out / "source.svg")


def draw_mapping(root: zarr.Group, index, out: Path) -> None:
    """One multi-channel stack of the built store, from source files to arrays."""
    multi = index[index["n_channels"] > 1]
    stack = (multi if len(multi) else index)["stack"].iloc[0]
    rows = index[index["stack"] == stack]
    first = rows.iloc[0]
    slots = first["channels"].split("-")
    types = first["channel_types"].split(",")
    n, h, w = len(rows), int(first["height"]), int(first["width"])
    c = d.Canvas(WIDTH)
    y = d.heading(c, f"How {stack} is built", f"{n:,} images, {len(slots)} channels, {w}x{h} pixels") + 30
    files = [d.Row(f"ch{s}", f"{n:,} PNGs ({t})") for s, t in zip(slots, types)]
    top = y
    d.panel(c, c.margin, top, 400, "CHAMMI-75_train/" + first["study"] + "/…", files,
            accent=d.ACCENTS["source"], source="one PNG per image and slot")
    meta_top = top + d.panel_height(len(files), source=True) + 24
    d.panel(c, c.margin, meta_top, 400, pipeline.METADATA_CSV,
            [d.Row("rows", f"{n * len(slots):,}"), d.Row("per image", "annotations, slot, type")],
            accent=d.ACCENTS["source"], source="one row per PNG")
    zip_top = meta_top + d.panel_height(2, source=True) + 24
    with_positions = int(rows["has_positions"].sum())
    d.panel(c, c.margin, zip_top, 400, pipeline.GUIDANCE_ZIP,
            [d.Row("read", f"{with_positions:,} files, lowest slot"),
             d.Row("cells", f"{int(rows['n_cells'].sum()):,}")],
            accent=d.ACCENTS["source"], source="one safetensors file per PNG")
    x = 620
    cube_y = top
    for k, (slot, kind) in enumerate(zip(slots[:3], types[:3])):
        g = d.CubeGeometry(x + k * 180, cube_y, 110, 80, 40)
        array = root[f"{stack}/ch{slot}/0"]
        d.cube(c, g, f"ch{slot}/0", [d.dims(array.shape), f"chunk {d.dims(array.chunks)}", "imagecodecs_png"],
               grid=([], []), depth_lines=[i / min(n, 8) for i in range(1, min(n, 8))], label_width=170)
        if k == 0:
            d.link(c, (c.margin + 400, top + 40), g.middle_left(), label="one PNG = one chunk")
    table_y = cube_y + 210
    d.panel(c, x, table_y, 420, "index/<column>", [d.Row("rows", f"{n:,} of {len(index):,}"),
                                                    d.Row("shape", "(image,)")])
    d.link(c, (c.margin + 400, meta_top + 40), (x, table_y + 30), label="one row per image")
    cells_y = table_y + d.panel_height(2) + 24
    d.panel(c, x, cells_y, 420, "cells/", [d.Row("position", "(cell, 2) uint16"),
                                            d.Row("background", "(image, 2) uint16")])
    d.link(c, (c.margin + 400, zip_top + 40), (x, cells_y + 30), label="entry 0 → background")
    c.write(out / "mapping.svg")


def draw_store(root: zarr.Group, index, out: Path) -> None:
    attrs = dict(root.attrs)
    contents = attrs.get("contents", {})
    c = d.Canvas(WIDTH)
    y = d.heading(c, "The store", attrs["title"]) + 30
    study = sorted(index["study"].unique())[0]
    stack = index.loc[index["study"] == study, "stack"].iloc[0]
    slot = index.loc[index["stack"] == stack, "channels"].iloc[0].split("-")[0]
    image = dict(root[f"{stack}/ch{slot}"].attrs)
    axes = image["ome"]["multiscales"][0]["coordinateSystems"][0]["axes"]
    panel_x = 520
    spec = [
        ("/", 0, None, "title, licenses, decisions, contents",
         [d.Row("studies", f"{contents.get('studies', 0):,}"), d.Row("images", f"{contents.get('images', 0):,}"),
          d.Row("PNGs", f"{contents.get('pngs', 0):,}"), d.Row("licenses", f"{CC_BY}, {CC0}")]),
        ("<study>/", 1, contents.get("studies"), "the study's license",
         [d.Row("license", dict(root[study].attrs)["annotations"]["license"]),
          d.Row("example", study)]),
        ("<stack>/", 2, contents.get("stacks"), "one size and channel set",
         [d.Row("example", stack.split("/", 1)[1]),
          d.Row("channels", index.loc[index["stack"] == stack, "channel_types"].iloc[0].replace(",", ", "))]),
        ("ch<slot>/0", 3, contents.get("channel_images"), "OME-Zarr image, one channel slot",
         [d.Row("axes", ", ".join(f"{a['name']}:{a['type']}" for a in axes)),
          d.Row("chunk", "one PNG"), d.Row("codec", "imagecodecs_png")]),
        ("index/<column>", 1, len(list(root["index"].array_keys())), "one row per image",
         [d.Row("rows", f"{len(index):,}"), d.Row("shape", "(image,)")]),
        ("cells/", 1, None, "positions on the image axis",
         [d.Row("cells", f"{root['cells/position'].shape[0]:,}"),
          d.Row("arrays", ", ".join(sorted(root["cells"].array_keys())))]),
        ("OME/", 1, None, "bioformats2raw series",
         [d.Row("series", f"{len(dict(root['OME'].attrs)['ome']['series']):,} images")]),
    ]
    nodes, cursor = [], y
    for name, depth, count, subtitle, rows in spec:
        nodes.append(d.TreeNode(name, cursor + 12, depth, count=count, subtitle=subtitle))
        d.panel(c, panel_x, cursor, 560, name, rows)
        cursor += d.panel_height(len(rows)) + 16
    d.tree(c, c.margin, nodes, leader_to=panel_x)
    c.write(out / "store.svg")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "diagrams")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    draw_source(source_counts(args.work), args.out)
    root = open_store(args.repo, args.branch)
    index = load_index(root)
    draw_mapping(root, index, args.out)
    draw_store(root, index, args.out)
    for name in ("source", "mapping", "store"):
        print(args.out / f"{name}.svg")


if __name__ == "__main__":
    main()
