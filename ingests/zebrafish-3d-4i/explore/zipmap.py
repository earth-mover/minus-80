"""Find out where everything sits in the S-BIAD3228 zip, and what each member is.

The zip is 591 GB and two million members. Converting it well depends on
knowing its layout: where each well's bytes are, in what order the arrays,
channels and planes come, where the metadata sits, and which members are
empty. This script answers those questions from the zip's central directory
and a few metadata documents per well, and decodes no pixels.

The code is in three parts:

- :mod:`inputs` loads the central directory and a well's metadata (fetched
  once, then cached);
- :mod:`layout` computes the layout from them, as pure functions over data
  frames; its docstring defines the terms used here (member, chunk, run,
  range, z index, padding);
- this module prints the answers on the command line, or writes them into
  an interactive page (``html``), built from ``map.html`` and
  ``map.js`` beside it.

Commands, run from the repository root::

    uv run --package zebrafish-3d-4i-ingest --group explore \\
        python ingests/zebrafish-3d-4i/explore/zipmap.py overview
    ... zipmap.py runs C/02                # a well's arrays and metadata, in file order
    ... zipmap.py order C/02/2/0           # the (channel, z index) order inside one array
    ... zipmap.py member C/02/2/0/1/13/0/7 # where one member is and what it covers
    ... zipmap.py field C/02 FOV_1         # the chunks one field needs, and their byte spans
    ... zipmap.py ranges C/02 --mib 64     # range requests to read a well
    ... zipmap.py meta C/02/2/.zattrs      # print one metadata document
    ... zipmap.py html --wells C/02,E/04   # written to the cache directory unless -o is given
"""

from __future__ import annotations

import argparse
import base64
import json
import math
from pathlib import Path

import numpy as np
import polars as pl

import layout
from inputs import CACHE, SOURCE, load_central_directory, load_directory_facts, load_member, load_well_metadata

HERE = Path(__file__).parent
#: Request latency and throughput measured on 2026-10-27, for the range estimates.
READ_SETTINGS = (
    ("R2 in-region", 0.100, 64, 584e6),
    ("EBI from London", 0.370, 16, 280e6),
)


def format_bytes(count: float) -> str:
    if count >= 1e8:
        return f"{count / 1e9:.2f} GB"
    if count >= 1e5:
        return f"{count / 1e6:.1f} MB"
    return f"{count / 1e3:.1f} kB"


def load_members(source: str) -> pl.DataFrame:
    facts = load_directory_facts(source)
    return layout.describe_members(load_central_directory(source), facts["directory_offset"])


def print_overview(source: str) -> None:
    """Member kinds, wells in file order, and the plate-level members between wells."""
    members = load_members(source)
    print(f"{len(members):,} members; the last one ends at {format_bytes(members['end'].max())}\n")
    kinds = members.group_by("kind").agg(
        pl.len().alias("members"), pl.sum("compressed").alias("zip bytes"), pl.sum("empty"), pl.sum("padding"),
    )
    print(kinds.sort("members", descending=True))
    wells = (
        members.filter(pl.col("well").is_not_null()).group_by("well")
        .agg(pl.min("start"), pl.max("end"), pl.len().alias("members"), pl.sum("empty")).sort("start")
    )
    print("\nwells in file order:")
    for well in wells.iter_rows(named=True):
        print(f"  {well['well']}  {well['start'] / 1e9:7.1f} – {well['end'] / 1e9:7.1f} GB  "
              f"{format_bytes(well['end'] - well['start']):>9}  {well['members']:>7,} members  {well['empty']:>3} empty")
    print("\nplate-level members, outside every well:")
    for member in members.filter(pl.col("well").is_null()).iter_rows(named=True):
        print(f"  {member['name']:<14} at {member['start'] / 1e9:7.1f} GB")


def print_runs(source: str, well: str) -> None:
    """A well's runs in file order: each array, metadata folder and table."""
    members = load_members(source).filter(pl.col("well") == well)
    well_start = members["start"].min()
    print(f"{well}: {len(members):,} members, bytes {well_start / 1e9:.2f} – {members['end'].max() / 1e9:.2f} GB\n")
    print(f"{'#':>3} {'start':>9} {'size':>10} {'members':>8} {'empty':>5} {'pad':>5}  what")
    for run in layout.runs(members).iter_rows(named=True):
        what = run["array"] if run["kind"] in ("image", "label") else f"[{run['kind']}] {run['array'] or '/'}"
        print(f"{run['run']:>3} {(run['start'] - well_start) / 1e9:8.3f}G {format_bytes(run['end'] - run['start']):>10}"
              f" {run['members']:>8,} {run['empty']:>5} {run['padding']:>5}  {what}")


def print_order(source: str, array: str) -> None:
    """The (channel, z index) groups inside one array's run, in file order."""
    chunks = layout.chunks_only(load_members(source)).filter(pl.col("array") == array)
    if chunks.is_empty():
        raise SystemExit(f"no chunks for {array}")
    groups = layout.chunk_order(chunks)
    run_start = chunks["start"].min()
    print(f"{array}: {len(chunks):,} chunks in {len(groups)} (channel, z index) groups, "
          f"{format_bytes(chunks['compressed'].sum())}\n")
    print(f"{'#':>4} {'at':>9} {'c':>2} {'z':>3} {'chunks':>6} {'size':>9}")
    for group in groups.iter_rows(named=True):
        channel = "-" if group["c"] is None else group["c"]
        print(f"{group['group_number']:>4} {(group['start'] - run_start) / 1e6:8.0f}M {channel:>2} {group['z']:>3}"
              f" {group['chunks']:>6} {format_bytes(group['compressed']):>9}")


def print_member(source: str, name: str) -> None:
    """Everything the directory says about one member, and which fields its grid cell serves."""
    members = load_members(source)
    found = members.filter(pl.col("name") == name)
    if found.is_empty():
        raise SystemExit(f"no member {name}")
    member = found.row(0, named=True)
    shown = {**member, "crc32": f"{member['crc32']:08x}", "flags": f"{member['flags']:#06x}"}
    for column in ("name", "kind", "start", "end", "compressed", "uncompressed", "crc32", "flags",
                   "well", "round", "label", "level", "c", "z", "y", "x"):
        value = shown[column]
        if value is not None:
            print(f"{column:>13}: {value:,}" if isinstance(value, int) else f"{column:>13}: {value}")
    if member["empty"]:
        print("       status: an empty file; reads as 0")
    elif member["padding"]:
        print("       status: an all-zero padding frame")
    if member["kind"] in ("image", "label"):
        metadata = load_well_metadata(load_central_directory(source), member["well"], source)
        well_start = members.filter(pl.col("well") == member["well"])["start"].min()
        first_plane = member["z"] * layout.PLANES_PER_CHUNK
        fields = layout.fields_in_cell(metadata, member["level"], member["y"], member["x"])
        print(f"  within the well: {(member['start'] - well_start) / 1e9:.3f} GB in, "
              f"in 64 MiB range #{(member['start'] - well_start) // (64 << 20)}")
        print(f"  planes {first_plane}–{first_plane + layout.PLANES_PER_CHUNK - 1}; "
              f"fields overlapping this grid cell: {', '.join(fields) or 'none'}")


def print_field(source: str, well: str, field_name: str, level: int) -> None:
    """The chunks one field needs at one level, and how many separate byte spans they make."""
    members = load_members(source)
    metadata = load_well_metadata(load_central_directory(source), well, source)
    field = next((field for field in metadata["fields"] if field["name"] == field_name), None)
    if field is None:
        raise SystemExit(f"{well} has fields {[field['name'] for field in metadata['fields']]}")
    window = layout.field_window(field, level, metadata["pixel_um"])
    cells = layout.grid_cells(window)
    print(f"{well} {field_name}: x {field['x_micrometer']} µm, y {field['y_micrometer']} µm")
    print(f"level {level}: rows {window[0]}, columns {window[1]} → grid cells {sorted(cells)}")
    chunks = layout.chunks_only(members).filter((pl.col("well") == well) & (pl.col("level") == level))
    in_cells = pl.Series([(row, column) in cells for row, column in zip(chunks["y"].to_list(), chunks["x"].to_list())])
    for (array,), array_chunks in chunks.filter(in_cells).group_by("array", maintain_order=True):
        spans = layout.merge_spans(array_chunks.sort("start"))
        print(f"  {array:<32} {len(array_chunks):>5} chunks {format_bytes(array_chunks['compressed'].sum()):>9}  "
              f"{len(spans):>4} separate byte spans: one request each if read alone")


def print_ranges(source: str, well: str, range_mib: int, gap_mib: int) -> None:
    """Range requests to read a well: the whole region, or only the chunks that must be decoded."""
    members = load_members(source).filter(pl.col("well") == well)
    metadata = load_well_metadata(load_central_directory(source), well, source)
    range_bytes = range_mib << 20
    region = members["end"].max() - members["start"].min()
    print(f"{well}: a region of {format_bytes(region)}")
    print(f"  the whole region in {range_mib} MiB ranges: {math.ceil(region / range_bytes):,} requests")
    chunks = layout.chunks_only(members)
    to_decode = chunks.filter(pl.Series(layout.needed(chunks, metadata)) & ~pl.col("padding") & ~pl.col("empty"))
    spans = layout.merge_spans(to_decode, gap=gap_mib << 20)
    span_bytes = sum(end - start for start, end in spans)
    requests = layout.range_count(spans, range_bytes)
    print(f"  only the chunks to decode: {len(to_decode):,} chunks, {format_bytes(to_decode['compressed'].sum())}")
    print(f"    merged across gaps up to {gap_mib} MiB: {len(spans):,} spans, {format_bytes(span_bytes)}, "
          f"{requests:,} requests of up to {range_mib} MiB")
    for where, latency, concurrency, throughput in READ_SETTINGS:
        waves = math.ceil(requests / concurrency)
        print(f"    {where:<16} {waves} waves × {latency * 1000:.0f} ms = {waves * latency:5.1f} s of waiting;"
              f" {span_bytes / throughput:5.1f} s of transfer at {throughput / 1e6:.0f} MB/s")


def print_document(source: str, key: str) -> None:
    """One metadata document, or the metadata member names that contain ``key``."""
    members = load_members(source)
    metadata_members = members.filter(pl.col("kind").is_in(["meta", "table"]))
    if key in set(metadata_members["name"].to_list()):
        print(json.dumps(json.loads(load_member(key, source)), indent=2))
        return
    matches = metadata_members.filter(pl.col("name").str.contains(key, literal=True))["name"]
    print("\n".join(matches.head(50).to_list()))
    if len(matches) > 50:
        print(f"... {len(matches)} matches")


def encode_column(values: np.ndarray) -> str:
    """A numeric column as base64 of its raw bytes, which the page decodes into a typed array."""
    return base64.b64encode(np.ascontiguousarray(values).tobytes()).decode()


def run_color_code(run: dict) -> int:
    """The page's color slot for a run: the round for an image run, 4 for labels, -1 for metadata."""
    if run["kind"] == "image":
        return int(run["array"].split("/")[2])
    return 4 if run["kind"] == "label" else -1


def well_page_data(members: pl.DataFrame, metadata: dict) -> dict:
    """Everything the page draws for one well: its runs, fields and every chunk."""
    well_start = int(members["start"].min())
    chunks = layout.chunks_only(members)
    arrays = chunks["array"].unique(maintain_order=True).to_list()
    array_index = {array: index for index, array in enumerate(arrays)}
    empty = chunks["empty"].to_numpy()
    padding = chunks["padding"].to_numpy()
    to_decode = layout.needed(chunks, metadata) & ~padding & ~empty
    # Three flags in one byte per chunk: 1 empty, 2 padding, 4 to decode.
    flags = empty.astype(np.uint8) | padding.astype(np.uint8) << 1 | to_decode.astype(np.uint8) << 2
    runs = []
    for run in layout.runs(members).iter_rows(named=True):
        is_chunk_run = run["kind"] in ("image", "label")
        parts = (run["array"] or "").split("/")
        runs.append({
            "array": run["array"], "kind": run["kind"], "members": run["members"],
            "empty": run["empty"], "padding": run["padding"],
            "start": run["start"] - well_start, "end": run["end"] - well_start,
            "round": int(parts[2]) if is_chunk_run else None, "level": int(parts[-1]) if is_chunk_run else None,
        })
    fields = []
    for field in metadata["fields"]:
        windows = [layout.field_window(field, level, metadata["pixel_um"]) for level in range(layout.LEVELS)]
        fields.append({"name": field["name"], "windows": [[*rows, *columns] for rows, columns in windows]})
    metadata_members = members.filter(~pl.col("kind").is_in(["image", "label"]))
    return {
        "start": well_start,
        "size": int(members["end"].max()) - well_start,
        "arrays": arrays,
        "array_kinds": ["label" if "/labels/" in array else "image" for array in arrays],
        "runs": runs,
        "metadata_members": [[row["name"], row["start"] - well_start] for row in metadata_members.iter_rows(named=True)],
        "fields": fields,
        "grid": metadata["grid"],
        "channels": metadata["channels"],
        "chunks": {
            "start": encode_column((chunks["start"] - well_start).to_numpy().astype(np.float64)),
            "bytes": encode_column((chunks["end"] - chunks["start"]).to_numpy().astype(np.uint32)),
            "compressed": encode_column(chunks["compressed"].to_numpy().astype(np.uint32)),
            "frame": encode_column(chunks["uncompressed"].to_numpy().astype(np.uint32)),
            "array": encode_column(np.array([array_index[array] for array in chunks["array"].to_list()], dtype=np.uint16)),
            "c": encode_column(chunks["c"].fill_null(-1).to_numpy().astype(np.int8)),
            "z": encode_column(chunks["z"].to_numpy().astype(np.int16)),
            "y": encode_column(chunks["y"].to_numpy().astype(np.int16)),
            "x": encode_column(chunks["x"].to_numpy().astype(np.int16)),
            "flags": encode_column(flags),
        },
    }


def write_page(source: str, wells_argument: str, out: Path) -> None:
    """Write the interactive map: the whole zip's runs, the directory, and every chunk of the chosen wells."""
    members = load_members(source)
    facts = load_directory_facts(source)
    wells = (
        members.filter(pl.col("well").is_not_null()).group_by("well")
        .agg(pl.min("start"), pl.max("end"), pl.len().alias("members")).sort("start")
    )
    chosen = wells["well"].to_list() if wells_argument == "all" else [w.strip() for w in wells_argument.split(",")]
    directory = load_central_directory(source)
    well_metadata = {well: load_well_metadata(directory, well, source) for well in chosen}
    page_data = {
        "first_well": chosen[0],
        "zip": {
            "size": facts["archive_size"],
            "wells": [
                {**well, "fields": len(well_metadata[well["well"]]["fields"]) if well["well"] in well_metadata else None,
                 "embedded": well["well"] in well_metadata}
                for well in wells.iter_rows(named=True)
            ],
            "runs": [[run["start"], run["end"], run_color_code(run)] for run in layout.runs(members).iter_rows(named=True)],
            "plate_members": [[row["name"], row["start"]] for row in members.filter(pl.col("well").is_null()).iter_rows(named=True)],
        },
        "directory": {
            "offset": facts["directory_offset"],
            "size": facts["directory_size"],
            "entries": len(members),
            "by_well": layout.directory_by_well(members),
        },
        "wells": {
            well: well_page_data(members.filter(pl.col("well") == well), well_metadata[well]) for well in chosen
        },
    }
    # "</" inside the embedded JSON would end the <script> element early.
    data = json.dumps(page_data).replace("</", "<\\/")
    page = (HERE / "map.html").read_text()
    page = page.replace("/*SCRIPT*/", (HERE / "map.js").read_text(), 1).replace("__DATA__", data, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    print(f"wrote {out} ({len(page) / 1e6:.1f} MB; wells: {', '.join(chosen)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=SOURCE, help="the zip's URL: EBI's, or a staged copy")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("overview", help="member kinds, wells in file order, plate-level members")
    commands.add_parser("runs", help="a well's arrays and metadata in file order").add_argument("well")
    commands.add_parser("order", help="the (channel, z index) order inside one array").add_argument("array")
    commands.add_parser("member", help="where one member is and what it covers").add_argument("name")
    field = commands.add_parser("field", help="the chunks one field needs, and their byte spans")
    field.add_argument("well")
    field.add_argument("field", help="FOV_1, FOV_2, ...")
    field.add_argument("--level", type=int, default=0)
    ranges = commands.add_parser("ranges", help="range requests to read a well")
    ranges.add_argument("well")
    ranges.add_argument("--mib", type=int, default=64, help="MiB per range request")
    ranges.add_argument("--gap", type=int, default=1, help="merge spans across gaps up to this many MiB")
    commands.add_parser("meta", help="print a metadata document, or list names containing the text").add_argument("key")
    html = commands.add_parser("html", help="write the interactive map, to open in a browser")
    html.add_argument("--wells", default="C/02,E/04", help="wells to embed, comma separated, or all (about 2 MB each)")
    html.add_argument("-o", "--out", type=Path, default=CACHE / "zip-map.html")
    args = parser.parse_args()

    if args.command == "overview":
        print_overview(args.source)
    elif args.command == "runs":
        print_runs(args.source, args.well)
    elif args.command == "order":
        print_order(args.source, args.array)
    elif args.command == "member":
        print_member(args.source, args.name)
    elif args.command == "field":
        print_field(args.source, args.well, args.field, args.level)
    elif args.command == "ranges":
        print_ranges(args.source, args.well, args.mib, args.gap)
    elif args.command == "meta":
        print_document(args.source, args.key)
    elif args.command == "html":
        write_page(args.source, args.wells, args.out)


if __name__ == "__main__":
    main()
