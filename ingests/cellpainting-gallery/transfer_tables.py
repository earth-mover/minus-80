"""Copy the gallery's Iceberg tables from earthmover-public into another catalog.

Each table in the ``cellpainting-gallery-tables`` namespace is created again with
the same schema and partition spec, and its current snapshot's data files are
appended one at a time, rows unchanged. History is not copied.

The ``images`` table is the exception: each row names the repo and snapshot that
serve it, and those are rewritten to the copy transfer.py made, so a reader opens
the new repo. Every row gets the copy's head of ``main``, which holds every plate.

A table whose row count already matches the source is skipped; one that does not
is dropped and copied again, so a stopped run resumes.

    uv run --package cellpainting-gallery-ingest python \\
        ingests/cellpainting-gallery/transfer_tables.py \\
        --catalog arraylake:<org> --repo arraylake://<org>/<repo>
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from transfer import batch_children, open_target
from minus80.core.icechunk_io import ARRAYLAKE
from minus80.core.progress import Progress

SOURCE_CATALOG = "earthmover-public"
NAMESPACE = "cellpainting-gallery-tables"
INDEX = "images"


def open_catalog(target: str):
    """A PyIceberg catalog: ``arraylake:<org>`` for Arraylake's, else a local directory."""
    if target.startswith("arraylake:"):
        from arraylake import Client

        return Client().get_iceberg(target.removeprefix("arraylake:"))
    from pyiceberg.catalog.sql import SqlCatalog

    root = Path(target).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return SqlCatalog("local", uri=f"sqlite:///{root}/catalog.db", warehouse=f"file://{root}")


def row_count(table) -> int:
    snapshot = table.current_snapshot()
    return int(snapshot.summary["total-records"]) if snapshot else 0


def repoint(rows: pa.Table, repo_name: str, snapshot: str, plates: set[str]) -> pa.Table:
    """``rows`` with ``repo`` and ``snapshot`` naming the copy instead of the source."""
    units = pc.binary_join_element_wise(
        rows["dataset"], rows["source"], rows["batch"], rows["plate"], "/")
    if missing := sorted(set(units.unique().to_pylist()) - plates):
        raise SystemExit(f"plates in {INDEX} that the copy does not hold: {missing}")
    for column, value in (("repo", repo_name), ("snapshot", snapshot)):
        rows = rows.set_column(rows.schema.get_field_index(column), rows.schema.field(column),
                               pa.array([value] * len(rows), pa.string()))
    return rows


def copy_table(source_catalog, catalog, name: str, rewrite) -> None:
    identifier = (NAMESPACE, name)
    source = source_catalog.load_table(identifier)
    want = row_count(source)
    tasks = list(source.scan().plan_files())
    if deletes := sum(len(t.delete_files) for t in tasks):
        raise SystemExit(f"{name} has {deletes} delete files; copying rows file by file would undo them")
    done = 0
    if catalog.table_exists(identifier):
        target = catalog.load_table(identifier)
        # Each append is one source file, in plan order, so a partial copy
        # resumes after its last append if the row counts agree.
        done = len(target.metadata.snapshots)
        if row_count(target) != sum(t.file.record_count for t in tasks[:done]):
            catalog.drop_table(identifier)
            done = 0
        elif done == len(tasks):
            print(f"  {name}: {want:,} rows already copied, skipped", flush=True)
            return
    if not done:
        catalog.create_table(identifier, schema=source.schema(), partition_spec=source.spec(),
                             sort_order=source.sort_order(), properties=source.properties)
    started = time.monotonic()
    size = sum(t.file.file_size_in_bytes for t in tasks[done:])
    resumed = f", resuming after {done}" if done else ""
    with Progress(f"copying {name} ({len(tasks)} files{resumed}, {want:,} rows)", size,
                  unit="MiB", scale=2**20, every=30) as progress:
        for task in tasks[done:]:
            # Reloaded per file: the catalog vends storage credentials that
            # expire (an hour on R2), and a large table outlasts them.
            io = source_catalog.load_table(identifier).io
            with io.new_input(task.file.file_path).open() as f:
                rows = pq.read_table(f)
            catalog.load_table(identifier).append(rewrite(rows) if rewrite else rows)
            progress.advance(task.file.file_size_in_bytes)
    got = row_count(catalog.load_table(identifier))
    if got != want:
        raise SystemExit(f"{name}: copied {got:,} rows of {want:,}")
    print(f"  {name}: {got:,} rows in {time.monotonic() - started:.1f} s", flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--catalog", required=True, help="arraylake:<org>, or a local directory")
    ap.add_argument("--repo", required=True,
                    help="the copy transfer.py made, which the images table is repointed at")
    ap.add_argument("--only", help="comma-separated table names; default all")
    args = ap.parse_args(argv)

    source_catalog = open_catalog(f"arraylake:{SOURCE_CATALOG}")
    catalog = open_catalog(args.catalog)
    catalog.create_namespace_if_not_exists(NAMESPACE)
    names = sorted(t[1] for t in source_catalog.list_tables(NAMESPACE))
    if args.only:
        wanted = args.only.split(",")
        if missing := set(wanted) - set(names):
            raise SystemExit(f"not in {SOURCE_CATALOG}.{NAMESPACE}: {sorted(missing)}")
        names = wanted
    # Smallest first, so a problem shows up before the 26 GiB cells table.
    sources = {n: source_catalog.load_table((NAMESPACE, n)) for n in names}
    names.sort(key=lambda n: row_count(sources[n]) and int(
        sources[n].current_snapshot().summary["total-files-size"]))
    repo_name = args.repo.removeprefix(ARRAYLAKE)
    for name in names:
        rewrite = None
        if name == INDEX:
            repo = open_target(args.repo)
            head = repo.lookup_branch("main")
            plates = set(asyncio.run(batch_children(repo.readonly_session(snapshot_id=head).store))[1])
            rewrite = lambda rows: repoint(rows, repo_name, head, plates)  # noqa: E731
        copy_table(source_catalog, catalog, name, rewrite)


if __name__ == "__main__":
    main()
