"""Draw the IBL Brain-Wide Map ingest: the two source hosts, how they map onto the store, and the store.

    MINUS80_S3_ANONYMOUS=1 uv run --package ibl-brain-wide-map-ingest python \\
        ingests/ibl-brain-wide-map/diagrams.py --repo <repo directory>

Writes ``source.svg``, ``mapping.svg``, ``store.svg`` and ``archive.svg`` into ``diagrams/``
beside this file. ``archive.py`` draws the last, the whole archive with pictures of its data,
and documents its own inputs. The first three are drawn for one session and probe (``EID``,
``PROBE``), and their values come from three inputs:

- the DANDI asset list (``sources.dandi_assets``) gives the dandiset's totals, the totals per
  kind of file and the session's raw and processed NWB sizes;
- IBL's two ONE caches (``sources.one_cache``) give the file count and size of each revision
  folder of the probe's spike sorting;
- the store, opened read-only, gives every attribute, shape, chunk shape and codec, whether a
  chunk is a reference or was written natively, the array count of each group and the spike
  count at each tag.

An array's shape, chunks and codec in the NWB file are the ones the store records, because the
store references the file's own chunks. No request goes to either host and no chunk is read.
LAYOUTS.md explains the mapping; the figures carry no prose.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import zarr
from archive import archive_figure
from ingest import OBJECTS, SORTS, SPIKE_ROWS_PER_CHUNK, session_record
from sources import dandi_assets, one_cache

from minus80 import diagrams as d
from minus80.core.icechunk_io import open_repository
from minus80.diagrams import ACCENTS, Canvas, CubeGeometry, Row, TreeNode

OUT = Path(__file__).parent / "diagrams"

EID = "465c44bd-2e67-4112-977b-36e1ac7e3f8c"
PROBE = "probe00"

# DANDI's asset paths do not say which kind of file they are, only the suffix does.
DANDI_KINDS = {
    "desc-raw_ecephys.nwb": "raw NWB",
    "desc-processed_behavior+ecephys.nwb": "processed NWB",
    "VideoLeftCamera.mp4": "left camera mp4",
    "VideoRightCamera.mp4": "right camera mp4",
    "VideoBodyCamera.mp4": "body camera mp4",
}

# IBL's ALF files that are neither one of the four referenced objects nor read into the store.
# clusters.uuids.csv is the one table the ingest reads (it holds strings, which a flat .npy
# reference cannot), so it is counted apart from the files left out.
UUIDS = "clusters.uuids.csv"


def kind_of(path: str) -> str:
    return next(label for suffix, label in DANDI_KINDS.items() if path.endswith(suffix))


def folder_files(cache: str, folder: str) -> list[tuple[str, int]]:
    """The ``(file name, size)`` of every file directly inside one folder of the probe's sort."""
    datasets = one_cache(cache)["datasets"].reset_index()
    rows = datasets[(datasets.eid == EID) & datasets.rel_path.str.startswith(folder)]
    return [(r.rel_path[len(folder):], int(r.file_size)) for r in rows.itertuples() if "/" not in r.rel_path[len(folder):]]


def classify(name: str) -> str:
    if name == UUIDS:
        return "read natively"
    if name.endswith(".npy") and name.split(".")[0] in OBJECTS:
        return "referenced"
    return "left out"


def dtype_text(array: zarr.Array) -> str:
    return "str" if array.dtype.kind == "T" else str(array.dtype)


def array_facts(array: zarr.Array) -> dict:
    """Shape, chunk shape and codec of a store array, as the figures print them."""
    codec = next((c for c in array.compressors if hasattr(c, "codec_config")), None)
    return {
        "shape": d.dims([d.count_text(n) for n in array.shape]),
        "chunks": d.dims([d.count_text(n) for n in array.chunks]),
        "count": math.prod(math.ceil(n / c) for n, c in zip(array.shape, array.chunks)),
        "codec": f"zlib {codec.codec_config['level']}" if codec else "none",
        "dtype": dtype_text(array),
    }


def chunk_kind(session, array: zarr.Array, index: list[int] | None = None) -> str:
    """Where an array's chunk lives: ``virtual`` (a reference), ``native``, ``inline`` or ``uninitialized``."""
    index = index if index is not None else [0] * array.ndim
    return str(session.chunk_type("/" + array.path, index)).rsplit(".", 1)[-1]


def last_chunk_kind(session, array: zarr.Array) -> str:
    return chunk_kind(session, array, [math.ceil(n / c) - 1 for n, c in zip(array.shape, array.chunks)])


def arrays_under(group: zarr.Group) -> list[zarr.Array]:
    return [member for _, member in group.members(max_depth=None) if isinstance(member, zarr.Array)]


def kind_counts(session, group: zarr.Group) -> dict[str, list[int]]:
    """The element count of every array under ``group``, by the kind of its first chunk."""
    counts: dict[str, list[int]] = {}
    for array in arrays_under(group):
        counts.setdefault(chunk_kind(session, array), []).append(math.prod(array.shape))
    return counts


def wording(kind: str) -> str:
    return "referenced" if kind == "virtual" else "native"


def session_group(session) -> zarr.Group:
    return zarr.open_group(session.store, mode="r")[session_record(EID)["group"]]


def nwb_asset(assets: list[dict], desc: str) -> dict:
    return next(a for a in assets if f"ses-{EID}_desc-{desc}" in a["path"])


def source_figure(repo, session) -> None:
    canvas = Canvas(width=1400)
    assets = dandi_assets()
    session_row = session_record(EID)
    path = f"alf/{PROBE}/pykilosort/"
    left, right, panel_w = 24, 800, 570
    group = session_group(session)
    bottom = d.heading(canvas, "Source: DANDI 000409 and the IBL bucket",
                       f"{session_row['group']} · {PROBE}")

    totals = {}
    for asset in assets:
        count, size = totals.get(kind_of(asset["path"]), (0, 0))
        totals[kind_of(asset["path"])] = (count + 1, size + asset["size"])
    rows = [Row("assets", f"{d.count_text(len(assets))} · {d.size_text(sum(a['size'] for a in assets))}")]
    rows += [Row(kind, f"{d.count_text(c)} · {d.size_text(s)}") for kind, (c, s) in totals.items()]
    y = d.panel(canvas, left, bottom + 20, 660, "dandiset 000409 @ 0.260309.1324", rows,
                accent=ACCENTS["source"], source="dandiarchive.org", key_width=130) + 28

    raw = nwb_asset(assets, "raw")
    processed = nwb_asset(assets, "processed")
    folder, raw_name = raw["path"].split("/")
    nodes = [TreeNode(folder + "/", y + 10, 0), TreeNode(raw_name, y + 40, 1, subtitle=d.size_text(raw["size"]))]
    array_rows = []
    for series in (f"ElectricalSeries{PROBE.capitalize()}AP", f"ElectricalSeries{PROBE.capitalize()}LF",
                   "TimeSeriesImec0Sync"):
        for array_name in ("data", "timestamps"):
            array = group[f"raw/acquisition/{series}/{array_name}"]
            facts = array_facts(array)
            array_rows.append(Row(f"{series}/{array_name}",
                                  f"{facts['shape']} {facts['dtype']} | {facts['chunks']} | {facts['codec']}"))
    nodes.append(TreeNode("acquisition/", y + 76, 2))
    y = d.panel(canvas, left + 70, y + 96, 700, "shape | chunks | codec", array_rows,
                accent=ACCENTS["source"], source=raw_name, key_width=330) + 20

    processed_name = processed["path"].split("/")[1]
    nodes.append(TreeNode(processed_name, y + 10, 1, subtitle=d.size_text(processed["size"])))
    processed_rows = [Row(name, f"{len(arrays_under(group['processed'][name]))} arrays")
                      for name in ("intervals/trials", "units")]
    processed_rows += [Row(f"processing/{name}", f"{len(arrays_under(member))} arrays")
                       for name, member in group["processed/processing"].groups()]
    d.panel(canvas, left + 70, y + 40, 700, "groups", processed_rows,
            accent=ACCENTS["source"], source=processed_name, key_width=330)
    d.tree(canvas, left, nodes)

    top = bottom + 20
    revisions = [("", "2022_Q4_IBL_et_al_BWM", "default revision"),
                 (SORTS["2024"]["revision"], SORTS["2024"]["cache"], "2024 re-sort")]
    d.tree(canvas, right, [
        TreeNode(f"{session_row['session_path']}/", top + 10, 0),
        TreeNode("alf/" + PROBE + "/pykilosort/", top + 40, 1)])
    cursor = top + 64
    for revision, cache, label in revisions:
        files = folder_files(cache, path + revision)
        by_class: dict[str, list[int]] = {}
        for file_name, size in files:
            by_class.setdefault(classify(file_name), []).append(size)
        rows = [Row("files", f"{len(files)} · {d.size_text(sum(s for _, s in files))}")]
        rows += [Row(kind, f"{len(sizes)} · {d.size_text(sum(sizes))}") for kind, sizes in sorted(by_class.items())]
        title = (revision.strip("/") or "pykilosort/") + (" (2022 sort)" if not revision else "")
        cursor = d.panel(canvas, right + 24, cursor, panel_w - 24, title, rows, accent=ACCENTS["source"],
                         source=f"{cache} ONE cache", key_width=130) + 16
    canvas.write(OUT / "source.svg")


def mapping_figure(repo, session) -> None:
    canvas = Canvas(width=1180)
    session_row = session_record(EID)
    group_path = session_row["group"]
    group = session_group(session)
    assets = dandi_assets()
    bottom = d.heading(canvas, "Mapping: source files to store groups", f"{group_path} · {PROBE}")

    series = f"ElectricalSeries{PROBE.capitalize()}"
    representatives = {
        "raw": [f"acquisition/{series}AP/data", f"acquisition/{series}LF/data",
                f"acquisition/{series}AP/timestamps", "general/extracellular_ephys/electrodes/id"],
        "processed": ["intervals/trials/start_time", "units/spike_times", "processing/wheel/WheelPosition/data",
                      "processing/pupil/LeftPupilDiameter/data"],
    }
    y = bottom + 24
    for name, paths in representatives.items():
        asset = nwb_asset(assets, name)
        counts = kind_counts(session, group[name])
        rows = []
        for relative in paths:
            if relative not in group[name]:
                continue
            array = group[name][relative]
            kind = chunk_kind(session, array)
            rows.append(Row(relative.removeprefix("acquisition/"),
                            f"{d.dims([d.count_text(n) for n in array.shape])} {dtype_text(array)}",
                            None if kind == "virtual" else "added", None if kind == "virtual" else "native"))
        file_rows = [Row("host", "DANDI"), Row("size", d.size_text(asset["size"])),
                     Row("arrays", f"{sum(map(len, counts.values()))} in the store")]
        file_height = d.panel_height(len(file_rows), source=True)
        group_height = d.panel_height(len(rows), source=True)
        mid = y + max(file_height, group_height) / 2
        d.panel(canvas, 24, mid - file_height / 2, 460, asset["path"].split("_desc-")[-1], file_rows,
                accent=ACCENTS["source"], source=f"{asset['path'].split('/')[0]}/", key_width=90)
        d.link(canvas, (484, mid), (620, mid), label="whole file")
        y = d.panel(canvas, 620, mid - group_height / 2, 540, f"{group_path}/{name}", rows,
                    accent=ACCENTS["store"], source=f"{sum(map(len, counts.values()))} arrays",
                    key_width=300) + 34

    # One entry per cube: where it comes from, where it sits in the store, how wide to draw it.
    # Cube width stays in proportion within a pair; the AP and LF bands are 12 times apart, which
    # would leave the LF cube too small to read.
    sorts = {sort: tagged_session(repo, sort) for sort in SORTS}
    spikes = {sort: tagged_times(sorts[sort]) for sort in SORTS}
    raw_name = nwb_asset(assets, "raw")["path"].split("_desc-")[-1]
    entries = [
        ("DANDI", raw_name, f"acquisition/{series}AP/data", f"raw/acquisition/{series}AP/data",
         group[f"raw/acquisition/{series}AP/data"], session, 260),
        ("DANDI", raw_name, f"acquisition/{series}LF/data", f"raw/acquisition/{series}LF/data",
         group[f"raw/acquisition/{series}LF/data"], session, 120),
    ]
    for sort, spec in SORTS.items():
        times = spikes[sort]
        entries.append(("IBL bucket", f"pykilosort/{spec['revision']}spikes.times.npy", "spikes.times",
                        f"alf/{PROBE}/pykilosort/spikes/times (sort-{sort})", times, sorts[sort],
                        100 * times.shape[0] / spikes["2022"].shape[0]))

    for host, file_name, array_path, store_path, array, array_session, width in entries:
        facts = array_facts(array)
        height = 80 if "data" in array_path else 14
        depth = 22 if height > 20 else 9
        geometry = CubeGeometry(620, y, width, height, depth)
        first, last = chunk_kind(array_session, array), last_chunk_kind(array_session, array)
        tail = array.shape[0] % array.chunks[0] if last != first else 0
        window = (1 - max(tail / array.shape[0], 0.012), 0, 1, 1) if tail else None
        cube_bottom = d.cube(canvas, geometry, store_path.removeprefix(f"{group_path}/"),
                             [f"{facts['shape']} {facts['dtype']}", f"chunks {facts['chunks']}",
                              f"{d.count_text(facts['count'])} chunks · {facts['codec']}"],
                             accent=ACCENTS["source"] if first == "virtual" else ACCENTS["store"],
                             window=window, label_width=420,
                             source=f"{wording(first)} chunks" + (f", last chunk {wording(last)}" if last != first else ""))
        mid = y + depth + height / 2
        d.panel(canvas, 24, mid - 36, 460, file_name, [Row("host", host), Row("array", array_path)],
                accent=ACCENTS["source"])
        d.link(canvas, (484, mid), geometry.middle_left(), label="referenced")
        y = max(cube_bottom, y + depth + height + 22) + 38

    small = []
    for name in representatives:
        counts = kind_counts(session, group[name])
        written = sum(len(v) for k, v in counts.items() if k in ("native", "inline"))
        largest = max((n for k, v in counts.items() if k in ("native", "inline") for n in v), default=0)
        small.append(Row(f"{name}/ small datasets", f"{written} arrays · up to {d.count_text(largest)} elements",
                         "added", "native"))
    native = small + [
        Row("clusters/uuids", "from clusters.uuids.csv", "added", "native"),
        Row("spikes/* last chunk", f"rows past a multiple of {d.count_text(SPIKE_ROWS_PER_CHUNK)}", "added", "native")]
    y = d.panel(canvas, 24, y, 700, "written into the store", native, accent=ACCENTS["store"],
                source="NWB strings, scalars and short arrays; clusters.uuids.csv; spikes .npy files", key_width=240)
    d.legend(canvas, 24, y + 20, [(ACCENTS["source"], "referenced"), (ACCENTS["highlight"], "native last chunk"),
                                  ("arrow", "source to store")])
    canvas.text(24, y + 62, "cube widths are not to scale between bands", size=11, color=d.MUTED)
    canvas.write(OUT / "mapping.svg")


def tagged_session(repo, sort: str):
    return repo.readonly_session(tag=f"sort-{sort}")


def tagged_times(session) -> zarr.Array:
    return session_group(session)[f"alf/{PROBE}/pykilosort/spikes/times"]


def summarize(value) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(value) + "}"
    if isinstance(value, list):
        return f"{len(value)} entries"
    return str(value)


def store_figure(repo, session) -> None:
    canvas = Canvas(width=1180)
    session_row = session_record(EID)
    group_path = session_row["group"]
    root = zarr.open_group(session.store, mode="r")
    group = root[group_path]
    series = group[f"raw/acquisition/ElectricalSeries{PROBE.capitalize()}AP"]
    sorting = group[f"alf/{PROBE}/pykilosort"]
    bottom = d.heading(canvas, "Store: the hierarchy", f"main at the {sorting.attrs['sort']} sort; "
                       f"{group_path} · {PROBE}")

    def attr_rows(attrs):
        return [Row(k, summarize(attrs[k])) for k in attrs]

    def group_rows(parent, names):
        return [Row(name, f"{len(arrays_under(parent[name]))} arrays") for name in names]

    def series_rows():
        rows = []
        for name, array in series.arrays():
            dimensions = ", ".join(array.metadata.dimension_names)
            rows.append(Row(name, f"{d.dims([d.count_text(n) for n in array.shape])} {dtype_text(array)} ({dimensions})"))
        return rows + attr_rows({k: v for k, v in series.attrs.items() if k == "spikeglx_meta"})

    def sorting_rows():
        return [Row(name, f"{len(list(sorting[name].arrays()))} arrays · "
                    + ", ".join(sorted(n for n, _ in sorting[name].arrays()))[:60]) for name in OBJECTS]

    raw_children = [name for name, _ in group["raw"].groups() if arrays_under(group["raw"][name])]
    processed_children = [name for name, _ in group["processed"].groups() if arrays_under(group["processed"][name])]
    items = [
        ("/", 0, attr_rows(root.attrs), None, "root attributes"),
        (group_path, 1, attr_rows(group.attrs), None, "session attributes"),
        ("raw", 2, group_rows(group["raw"], raw_children), f"{len(arrays_under(group['raw']))} arrays", "raw"),
        (series.name.rsplit("/", 1)[-1], 3, series_rows(), f"{len(arrays_under(series))} arrays", "acquisition series"),
        ("processed", 2, group_rows(group["processed"], processed_children),
         f"{len(arrays_under(group['processed']))} arrays", "processed"),
        (f"alf/{PROBE}/pykilosort", 2, attr_rows(sorting.attrs) + group_rows(sorting, list(OBJECTS)),
         f"{len(arrays_under(sorting))} arrays", "pykilosort"),
    ]

    y, nodes, panel_args = bottom + 24, [], []
    for name, depth, rows, subtitle, title in items:
        nodes.append(TreeNode(name, y + 10, depth, subtitle=subtitle))
        panel_args.append((y, title, rows))
        y += max(d.panel_height(len(rows)) + 14, 50)
    for top, title, rows in panel_args:
        d.panel(canvas, 480, top, 670, title, rows, key_width=230)
    d.tree(canvas, 24, nodes, leader_to=480)

    tag_rows = []
    for sort in SORTS:
        times = tagged_times(tagged_session(repo, sort))
        tag_rows.append(Row(f"sort-{sort}", f"{d.count_text(times.shape[0])} spikes · revision "
                            f"{SORTS[sort]['revision'].strip('/#') or 'default'}"))
    d.panel(canvas, 480, y + 10, 670, "tags", tag_rows, key_width=230)
    canvas.write(OUT / "store.svg")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True, help="directory or URL of the store to draw")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    repo = open_repository(args.repo)
    session = repo.readonly_session("main")
    for figure in (source_figure, mapping_figure, store_figure):
        figure(repo, session)
    archive_figure(repo, OUT)
    print(f"wrote {', '.join(sorted(p.name for p in OUT.glob('*.svg')))} to {OUT.name}/")


if __name__ == "__main__":
    main()
