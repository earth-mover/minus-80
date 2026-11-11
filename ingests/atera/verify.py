"""Check an Atera fixture repo and its Iceberg catalog, local or on Arraylake, against the bundle, gate by gate.

Each gate prints PASS, or FAIL and its mismatches. The script exits non-zero on any failure.
The ``matrix`` and ``tables`` gates hold the kidney's shape and the fixture's tile ``0,0``,
so they check the kidney fixture only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import scipy.sparse
import shapely
import zarr
from anndata.io import read_elem
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, open_repo
from minus80.core.progress import Progress
from minus80.core.pyramid import check_page
from minus80.core.tables import local_catalog, parquet_field_ids
from minus80.core.zarr_copy import read_array
from pyiceberg.schema import index_by_name
from zarr.core.buffer import default_buffer_prototype
from zarr.core.sync import sync

from bundle import CELLS, MATRIX, MORPHOLOGY_2D
from ingest import CELL_SET, FIXTURE_LEVEL, INSTANCE_KEY, MASKS, MIB, NUCLEUS_SET, TABLE_REGION, open_bundle
from sources import S3, SAMPLES
from transcripts import xenium_cell_id

SAMPLED = 50
MATRIX_CHUNKS = 20
IMAGE_TILES = 4
#: Half the side of the window that holds a sampled label, in pixels: 32 micrometres.
WINDOW = 150
SEED = 0
#: The polygon set that each shapes element holds.
POLYGON_SETS = {"shapes/nucleus_boundaries": NUCLEUS_SET, "shapes/cell_boundaries": CELL_SET}


def raw(store, key: str) -> bytes | None:
    buffer = sync(store.get(key, default_buffer_prototype()))
    return None if buffer is None else buffer.to_bytes()


def chunk_members(zf, prefix: str) -> list[str]:
    stem = f"{prefix}/"
    return [n for n in zf.namelist() if n.startswith(stem) and n[len(stem):].replace(".", "").isdigit()]


def chunk_key(array_path: str, member: str) -> str:
    return f"{array_path}/c/" + member.rsplit("/", 1)[1].replace(".", "/")


def gate_images(root, bundle) -> list[str]:
    array, errors = root[f"images/morphology/s{FIXTURE_LEVEL}"], []
    for channel, name in enumerate(MORPHOLOGY_2D):
        page = bundle.tiff(name).series[0].levels[FIXTURE_LEVEL].pages[0]
        grid = [(r, c) for r in range(math.ceil(page.imagelength / page.tilelength))
                for c in range(math.ceil(page.imagewidth / page.tilewidth))]
        tiles = [grid[i] for i in np.linspace(0, len(grid) - 1, min(IMAGE_TILES, len(grid))).astype(int)]
        print(f"  images: channel {channel}, {len(tiles)} of {len(grid)} tiles checked")
        errors += [f"channel {channel}: {e}" for e in check_page(array, page, (channel,), tiles)]
    return errors


def gate_labels(root, cells) -> list[str]:
    errors = []
    for name, mask in MASKS.items():
        members = chunk_members(cells, mask)
        path = f"labels/{name}/s0"
        if not members:
            errors.append(f"{mask} has no chunk members")
        # icechunk lists only from a node path, so list the array and keep its chunk keys.
        stored = sum(k.startswith(f"{path}/c/") for k in sync(_list(root.store, path)))
        if stored != len(members):
            errors.append(f"{path}: {stored} chunks stored, {len(members)} members")
        with Progress(f"labels: comparing {len(members):,} chunks of {mask}", total=len(members), unit="chunks") as p:
            for member in members:
                if raw(root.store, chunk_key(path, member)) != cells.read(member):
                    errors.append(f"{path}: chunk {member} differs")
                p.advance()
    return errors


async def _list(store, prefix):
    return [k async for k in store.list_prefix(prefix)]


def gate_matrix(root, matrix) -> list[str]:
    errors = []
    x = read_elem(root["tables/table/X"])
    print(f"  matrix: {type(x).__module__}.{type(x).__name__}, shape {x.shape}, {x.nnz:,} stored values")
    if not scipy.sparse.issparse(x) or x.format != "csr":
        errors.append(f"X is {type(x).__name__}, not CSR")
    if x.shape != (399120, 24674) or x.nnz != 599212411:
        errors.append(f"X has shape {x.shape} and {x.nnz} stored values")
    del x
    rng = np.random.default_rng(SEED)
    for part in ("data", "indices"):
        members = chunk_members(matrix, f"X/{part}")
        for i in rng.choice(len(members), MATRIX_CHUNKS // 2, replace=False):
            if raw(root.store, chunk_key(f"tables/table/X/{part}", members[i])) != matrix.read(members[i]):
                errors.append(f"X/{part}: chunk {members[i]} differs")
    print(f"  matrix: {MATRIX_CHUNKS} sampled chunks of X/data and X/indices compared")
    return errors


def element_tables(root) -> dict[str, dict]:
    """``iceberg_table`` attribute by element path."""
    found = {}
    for kind in ("points", "shapes"):
        for name, group in root[kind].groups():
            found[f"{kind}/{name}"] = dict(group.attrs["iceberg_table"])
    return found


def load(catalog, attr):
    return catalog.load_table(f"{attr['namespace']}.{attr['table']}")


def pinned_files(catalog, attr) -> list:
    """The data files of the snapshot that ``attr`` pins, not of the current snapshot."""
    return [task.file for task in load(catalog, attr).scan(snapshot_id=attr["snapshot_id"]).plan_files()]


def gate_tables(root, catalog, cells, transcripts) -> list[str]:
    errors = []
    for path, attr in element_tables(root).items():
        # A polygon table has one row per cell with a polygon.
        if path in POLYGON_SETS:
            expected = len(np.unique(read_array(cells, f"polygon_sets/{POLYGON_SETS[path]}/cell_index")))
        else:
            expected = json.loads(transcripts.read("grid/0,0/location/.zarray"))["shape"][0]
        rows = sum(f.record_count for f in pinned_files(catalog, attr))
        print(f"  tables: {path} has {rows:,} rows, the source {expected:,}")
        if rows != expected:
            errors.append(f"{path}: {rows} rows, source {expected}")
    return errors


def gate_field_ids(root, catalog) -> list[str]:
    """Each data file of the pinned snapshot states every field id of that snapshot's schema.

    The index column is required in that schema and non-nullable in each file.
    """
    attrs = element_tables(root)
    errors = [] if attrs else ["no element has an iceberg_table attribute"]
    for path, attr in attrs.items():
        table, index, snapshot_id = load(catalog, attr), attr["index_column"], attr["snapshot_id"]
        snapshot = table.snapshot_by_id(snapshot_id)
        if snapshot is None:
            errors.append(f"{path}: the table has no snapshot {snapshot_id}")
            continue
        schema = table.schemas()[snapshot.schema_id]
        expected = index_by_name(schema)
        if not schema.find_field(index).required:
            errors.append(f"{path}: {index} is optional in the table schema")
        files = [task.file.file_path for task in table.scan(snapshot_id=snapshot_id).plan_files()]
        if not files:
            errors.append(f"{path}: snapshot {snapshot_id} has no data files")
        for f in files:
            with table.io.new_input(f).open() as stream:
                footer = pq.read_schema(stream)
            found = parquet_field_ids(footer)
            if extra := [n for n in found if n not in expected]:
                errors.append(f"{path}: {f} has columns the table lacks: {', '.join(extra)}")
            if wrong := [n for n, i in found.items() if n in expected and i != expected[n]]:
                errors.append(f"{path}: {f} has no field id, or another, for {', '.join(wrong)}")
            if lacking := [n for n in expected if n not in found]:
                errors.append(f"{path}: {f} lacks fields {', '.join(lacking)}")
            if index in footer.names and footer.field(index).nullable:
                errors.append(f"{path}: {f} has a nullable {index}")
        print(f"  field_ids: {path}: snapshot {snapshot_id}, {len(files)} files, {len(expected)} fields")
    return errors


def gate_table_index(root) -> list[str]:
    """``obs`` and ``var`` list their index array only as ``_index``, and anndata reads them."""
    errors = []
    for name in ("obs", "var"):
        group = root[f"tables/table/{name}"]
        index = group.attrs["_index"]
        if index in group.attrs["column-order"]:
            errors.append(f"{name}: index {index!r} is also in column-order")
        try:
            frame = read_elem(group)
        except Exception as e:  # The gate reports any reader failure as a mismatch.
            errors.append(f"{name}: read_elem raised {type(e).__name__}: {e}")
            continue
        print(f"  table_index: {name} reads as {frame.shape[0]:,} rows, {frame.shape[1]} columns, index {index!r}")
    return errors


def _pixel(micrometres: float, pixel_size: float) -> int:
    return int(micrometres / pixel_size)


def _area(labels, row: int, col: int, pixel_size: float) -> tuple[int, float]:
    """The label at ``(row, col)`` and its area in square micrometres within the window."""
    window = labels[max(row - WINDOW, 0):row + WINDOW, max(col - WINDOW, 0):col + WINDOW]
    label = int(labels[row, col])
    return label, float((window == label).sum()) * pixel_size**2 if label else math.nan


def gate_masks(root, summary, columns, pixel_size) -> list[str]:
    nucleus, cell = columns.index("nucleus_area"), columns.index("cell_area")
    ny, nx = columns.index("nucleus_centroid_y"), columns.index("nucleus_centroid_x")
    # A cell without a nucleus has NaN nucleus columns.
    with_nucleus = np.flatnonzero(np.isfinite(summary[:, [nucleus, ny, nx]]).all(axis=1))
    print(f"  masks: {len(summary) - len(with_nucleus):,} of {len(summary):,} cell_summary rows have no nucleus")
    rows = np.random.default_rng(SEED).choice(with_nucleus, SAMPLED, replace=False)
    errors = {}
    for name in MASKS:
        labels = root[f"labels/{name}/s0"]
        areas = np.array([_area(labels, _pixel(summary[r, ny], pixel_size), _pixel(summary[r, nx], pixel_size),
                                pixel_size)[1] for r in rows])
        for column, index in (("nucleus_area", nucleus), ("cell_area", cell)):
            relative = np.abs(areas - summary[rows, index]) / summary[rows, index]
            errors[name, column] = float(np.nanmedian(relative))
        print(f"  masks: {name}: median label area {np.nanmedian(areas):.2f} um2, background at "
              f"{int(np.isnan(areas).sum())} of {SAMPLED} nucleus centroids")
    print(f"  masks: cell_summary medians: nucleus_area {np.median(summary[rows, nucleus]):.2f} um2, "
          f"cell_area {np.median(summary[rows, cell]):.2f} um2")
    for (name, column), error in errors.items():
        print(f"  masks: {name} vs {column}: median relative error {error:.3f}")
    failures = []
    if not errors["nucleus_labels", "nucleus_area"] < errors["cell_labels", "nucleus_area"]:
        failures.append("nucleus_area agrees better with cell_labels than with nucleus_labels")
    if not errors["cell_labels", "cell_area"] < errors["nucleus_labels", "cell_area"]:
        failures.append("cell_area agrees better with nucleus_labels than with cell_labels")
    return failures


def gate_instances(root, summary, columns, cell_id, pixel_size) -> list[str]:
    rows = np.random.default_rng(SEED + 1).choice(len(summary), SAMPLED, replace=False)
    cy, cx = columns.index("cell_centroid_y"), columns.index("cell_centroid_x")
    labels = root["labels/cell_labels/s0"]
    obs_cell_id = root["tables/table/obs/cell_id"][:]
    instance = root[f"tables/table/obs/{INSTANCE_KEY}"][:]
    errors = []
    for r in rows:
        label = int(labels[_pixel(summary[r, cy], pixel_size), _pixel(summary[r, cx], pixel_size)])
        pair = (int(obs_cell_id[r]) & 0xFFFFFFFF, int(obs_cell_id[r]) >> 32)
        if label != r + 1:
            errors.append(f"row {r}: cell_labels at the cell centroid is {label}, not {r + 1}")
        if pair != tuple(int(v) for v in cell_id[r]):
            errors.append(f"row {r}: obs.cell_id {obs_cell_id[r]} is {pair}, cells cell_id is {tuple(cell_id[r])}")
        if instance[r] != r + 1:
            errors.append(f"row {r}: obs.{INSTANCE_KEY} is {instance[r]}, not {r + 1}")
    print(f"  instances: {SAMPLED} rows, {len(errors)} mismatches")
    return errors


def pinned_columns(catalog, attr, columns: tuple[str, ...]):
    """The ``columns`` of the snapshot that ``attr`` pins, as an Arrow table."""
    return load(catalog, attr).scan(snapshot_id=attr["snapshot_id"], selected_fields=columns).to_arrow()


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def gate_polygons(root, catalog, summary, columns) -> list[str]:
    """Each polygon set: unique ``instance``, the invalid parts against the metrics, and, for the nucleus set, ``part_count``."""
    errors = []
    for path, attr in ((p, a) for p, a in element_tables(root).items() if p in POLYGON_SETS):
        table = pinned_columns(catalog, attr, ("instance", "cell_index", "part_count", "geometry"))
        values, counts = np.unique(table["instance"].to_numpy(), return_counts=True)
        if len(repeated := values[counts > 1]):
            errors.append(f"{path}: {_plural(len(repeated), 'instance')} repeat{'s' if len(repeated) == 1 else ''}, first {repeated[0]}")
        parts = shapely.get_parts(shapely.from_wkb(table["geometry"].to_numpy(zero_copy_only=False)))
        invalid = int((~shapely.is_valid(parts)).sum())
        print(f"  polygons: {invalid:,} of {len(parts):,} {path} parts are not valid")
        if path == "shapes/nucleus_boundaries":
            errors += _gate_part_count(path, table, summary, columns)
            stated = root.attrs["metrics"]["number_of_nucleus_non_simple_polygons"]
            if stated is None:
                # 10x leaves the metric empty, so there is nothing to disagree with.
                print(f"  polygons: FINDING {invalid:,} invalid nucleus polygons; the metrics CSV states no count")
            elif invalid != stated:
                errors.append(f"{invalid} invalid nucleus polygons, the metrics CSV states {stated}")
    return errors


def _gate_part_count(path: str, table, summary, columns) -> list[str]:
    """``part_count`` against ``nucleus_count`` of ``cell_summary`` on a sample of cells; a cell with no row has 0 parts."""
    rows = np.random.default_rng(SEED + 2).choice(len(summary), min(SAMPLED, len(summary)), replace=False)
    held = dict(zip(table["cell_index"].to_pylist(), table["part_count"].to_pylist()))
    stated = summary[:, columns.index("nucleus_count")]
    differ = sorted(int(r) for r in rows if held.get(int(r), 0) != stated[r])
    print(f"  polygons: {path} part_count differs from nucleus_count on {len(differ)} of {len(rows)} sampled cells")
    if not differ:
        return []
    return [f"{path}: part_count differs from nucleus_count on {len(differ)} of {len(rows)} sampled cells, "
            f"first cell_index {differ[0]}"]


def gate_table_region(root, catalog) -> list[str]:
    """The table's ``instance`` set equals ``cell_boundaries``'; ``cell_id`` agrees on a sample; the region is ``cell_boundaries``."""
    attrs = element_tables(root)
    if "shapes/cell_boundaries" not in attrs:
        return ["no shapes/cell_boundaries element"]
    errors, table = [], root["tables/table"]
    polygons = pinned_columns(catalog, attrs["shapes/cell_boundaries"], ("instance", "cell_id"))
    shape_instance, instance = polygons["instance"].to_numpy(), table[f"obs/{INSTANCE_KEY}"][:]
    for noun, only, other in (("obs.instance", np.setdiff1d(instance, shape_instance), "cell_boundaries"),
                              ("cell_boundaries", np.setdiff1d(shape_instance, instance), "obs.instance")):
        if len(only):
            errors.append(f"{noun} has {_plural(len(only), 'instance')} that {other} lacks, first {only[0]}")
    known = np.flatnonzero(np.isin(instance, shape_instance))
    rows = np.random.default_rng(SEED + 3).choice(known, min(SAMPLED, len(known)), replace=False)
    packed = table["obs/cell_id"][:][rows]
    # obs.cell_id packs the pair as prefix + suffix * 2**32.
    obs_ids = xenium_cell_id(packed & 0xFFFFFFFF, packed >> 32)
    shape_ids = dict(zip(shape_instance.tolist(), polygons["cell_id"].to_pylist()))
    differ = sorted(int(instance[r]) for r, a in zip(rows, obs_ids) if shape_ids[int(instance[r])] != a)
    if differ:
        errors.append(f"cell_id differs on {len(differ)} of {len(rows)} sampled instances, first instance {differ[0]}")
    for label, found in (
        ("region attribute", table.attrs.get("region")),
        ("uns region", table["uns/spatialdata_attrs/region"][()]),
    ):
        if found != TABLE_REGION:
            errors.append(f"tables/table: {label} is {found!r}")
    regions = {str(r) for r in read_elem(table["obs/region"])}
    if regions != {TABLE_REGION}:
        errors.append(f"tables/table: obs.region holds {', '.join(repr(r) for r in sorted(regions))}")
    return errors


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gate_copies(root, catalog) -> list[str]:
    errors = []
    for path, attr in element_tables(root).items():
        table = load(catalog, attr)
        files = [f.file_path for f in pinned_files(catalog, attr)]
        registered = Counter(_sha(table.io.new_input(f).open().read()) for f in files)
        arrays = [a for _, a in root[path].arrays() if "file:name" in a.attrs]
        copied = Counter(_sha(a[:].tobytes()) for a in arrays)
        print(f"  copies: {path}: {len(arrays)} uint8 arrays, {len(files)} registered files")
        if copied != registered:
            errors.append(f"{path}: the uint8 arrays' bytes differ from the registered files")
    return errors


def gate_attrs(root, catalog) -> list[str]:
    attrs = element_tables(root)
    print(f"  attrs: {len(attrs)} iceberg_table attributes")
    errors = [] if attrs else ["no element has an iceberg_table attribute"]
    for path, attr in attrs.items():
        table = load(catalog, attr)
        if str(table.metadata.table_uuid) != attr["table_uuid"]:
            errors.append(f"{path}: attribute pins table {attr['table_uuid']}, catalog has {table.metadata.table_uuid}")
        elif table.snapshot_by_id(attr["snapshot_id"]) is None:
            errors.append(f"{path}: the table has no snapshot {attr['snapshot_id']}")
    return errors


def open_root(location: str, branch: str) -> zarr.Group:
    """The branch read-only; both openers authorize the repo's virtual chunk containers."""
    if location.startswith(ARRAYLAKE):
        return zarr.open_group(arraylake_repository(location).readonly_session(branch).store, mode="r")
    return open_repo(location, branch=branch)


def open_catalog(location: str):
    if location.startswith("arraylake:"):
        from arraylake import Client

        return Client().get_iceberg(location.removeprefix("arraylake:"))
    return local_catalog(Path(location))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="a local directory, or arraylake://<org>/<repo>")
    parser.add_argument("--catalog", required=True, help="a local catalog directory, or arraylake:<org>")
    parser.add_argument("--branch", default="fixture")
    parser.add_argument("--sample", choices=sorted(SAMPLES), default="kidney")
    parser.add_argument("--bundle", help="a local outs.zip; by default the bundle is read from 10x's bucket")
    parser.add_argument("--gates", nargs="+", help="run only these gates")
    args = parser.parse_args()

    # 10x's bucket is public, and a signed request without credentials fails.
    os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")
    zarr.config.set({"array.rectilinear_chunks": True})
    root = open_root(args.repo, args.branch)
    catalog = open_catalog(args.catalog)
    bundle, counter = open_bundle(args.bundle or SAMPLES[args.sample].url("_outs.zip", S3))
    cells, matrix = bundle.member_zip(CELLS), bundle.member_zip(MATRIX)
    transcripts = bundle.member_zip("transcripts.zarr.zip")
    pixel_size = bundle.experiment()["pixel_size"]
    names = set(cells.namelist())
    summary = read_array(cells, "cell_summary", names=names)
    columns = json.loads(cells.read("cell_summary/.zattrs"))["column_names"]
    cell_id = read_array(cells, "cell_id", names=names)

    gates = {
        "images": lambda: gate_images(root, bundle),
        "labels": lambda: gate_labels(root, cells),
        "matrix": lambda: gate_matrix(root, matrix),
        "tables": lambda: gate_tables(root, catalog, cells, transcripts),
        "masks": lambda: gate_masks(root, summary, columns, pixel_size),
        "instances": lambda: gate_instances(root, summary, columns, cell_id, pixel_size),
        "polygons": lambda: gate_polygons(root, catalog, summary, columns),
        "table_region": lambda: gate_table_region(root, catalog),
        "copies": lambda: gate_copies(root, catalog),
        "attrs": lambda: gate_attrs(root, catalog),
        "field_ids": lambda: gate_field_ids(root, catalog),
        "table_index": lambda: gate_table_index(root),
    }
    failed = []
    for name in args.gates or gates:
        gate = gates[name]
        errors = gate()
        print(f"{name} {'FAIL' if errors else 'PASS'}", flush=True)
        for error in errors[:20]:
            print(f"  {error}")
        if errors:
            failed.append(name)
    print(f"bundle: {counter.bytes / MIB:,.1f} MiB read")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
