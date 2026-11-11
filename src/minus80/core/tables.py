"""Iceberg tables from Parquet files, and their ``uint8`` copies in an Icechunk repo.

The ingest writes each Parquet file itself, so the footer carries GeoParquet
``geo`` and pandas index metadata, and registers the files with ``add_files``.
:func:`declare_copy` then references the same files from the repo, so a Python
reader sees the file and a reader of the ``iceberg_table`` attribute uses the table.

A copy's last chunk is shorter than the others, so its grid is rectilinear. A reader
must set ``zarr.config.set({"array.rectilinear_chunks": True})`` to open it.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import NamedTuple

import pyarrow as pa
import pyarrow.parquet as pq
import zarr

#: The ``file:`` Zarr convention, as its convention document states it.
FILE_CONVENTION = {
    "uuid": "6cdb4f72-1a6a-4ca0-a008-f4ff70bd0024",
    "name": "file:",
    "description": "This array's elements are the bytes of a file; its node path is the file's path.",
}


def declare_file(array: zarr.Array, name: str) -> None:
    """Mark a 1-D ``uint8`` array as the file ``name`` under the ``file:`` convention."""
    if array.ndim != 1:
        raise ValueError(f"{array.path} is not 1-D, so it cannot be a file")
    if array.dtype != "uint8":
        raise ValueError(f"{array.path} has dtype {array.dtype}, a file needs uint8")
    if name != array.basename:
        raise ValueError(f"file name {name!r} is not the node name {array.basename!r}")
    array.update_attributes(
        {"zarr_conventions": [FILE_CONVENTION], "file:name": name}
    )


def write_parquet(
    table: pa.Table,
    path: str,
    *,
    index_column: str,
    geo: Mapping | None = None,
    row_group_size: int = 1_000_000,
) -> int:
    """Write ``table`` to ``path`` with pandas index and optional ``geo`` footer metadata.

    Returns the file size in bytes.
    """
    # pyarrow describes the columns from an empty frame, so no data is copied.
    frame = table.schema.empty_table().to_pandas()
    pandas_meta = json.loads(pa.Schema.from_pandas(frame, preserve_index=False).metadata[b"pandas"])
    pandas_meta["index_columns"] = [index_column]
    metadata = dict(table.schema.metadata or {})
    metadata[b"pandas"] = json.dumps(pandas_meta).encode()
    if geo is not None:
        metadata[b"geo"] = json.dumps(geo).encode()
    pq.write_table(
        table.replace_schema_metadata(metadata), path, row_group_size=row_group_size
    )
    with open(path, "rb") as f:
        return f.seek(0, 2)


class TableRef(NamedTuple):
    """An Iceberg table snapshot and the size of each registered file."""

    table_uuid: str
    snapshot_id: int
    files: list[tuple[str, int]]


def require_table(
    catalog,
    identifier: str,
    *,
    schema: pa.Schema,
    properties: Mapping[str, str],
    partition_by: str | None = None,
):
    """The table ``identifier``, created when absent and partitioned by identity on ``partition_by``."""
    if catalog.table_exists(identifier):
        return catalog.load_table(identifier)
    table = catalog.create_table(identifier, schema=schema, properties=dict(properties))
    if partition_by is not None:
        with table.update_spec() as spec:
            spec.add_identity(partition_by)
    return table


def register_files(
    catalog,
    identifier: str,
    files: Sequence[str],
    *,
    schema: pa.Schema,
    properties: Mapping[str, str],
    partition_by: str | None = None,
) -> TableRef:
    """Add ``files`` to the table ``identifier``, creating it when absent.

    A new table is partitioned by identity on ``partition_by``. A file the current snapshot
    already holds is not added again, so a rerun returns the same snapshot.
    """
    table = require_table(catalog, identifier, schema=schema, properties=properties,
                          partition_by=partition_by)
    held = {task.file.file_path for task in table.scan().plan_files()}
    new = [f for f in files if f not in held]
    if new:
        table.add_files(new)
        table = catalog.load_table(identifier)
    sizes = [(f, len(table.io.new_input(f))) for f in files]
    return TableRef(
        str(table.metadata.table_uuid), table.current_snapshot().snapshot_id, sizes
    )


def iceberg_table_attr(
    ref: TableRef,
    *,
    catalog: str = "arraylake",
    org: str,
    namespace: str,
    table: str,
    index_column: str,
    geometry_column: str | None = None,
) -> dict:
    """The ``iceberg_table`` attribute that pins ``ref``'s table and snapshot."""
    attr = {
        "catalog": catalog,
        "org": org,
        "namespace": namespace,
        "table": table,
        "table_uuid": ref.table_uuid,
        "snapshot_id": ref.snapshot_id,
        "index_column": index_column,
    }
    if geometry_column is not None:
        attr["geometry_column"] = geometry_column
    return attr


def _copy_array(
    group: zarr.Group, path: str, location: str, size: int, chunk: int
) -> zarr.Array:
    import icechunk

    edges = [chunk] * (size // chunk) + ([size % chunk] if size % chunk else [])
    array = group.create_array(
        path, shape=(size,), chunks=[edges] if edges else (1,), dtype="uint8",
        compressors=None, filters=None, overwrite=True,
    )
    declare_file(array, array.basename)
    specs = [
        icechunk.VirtualChunkSpec(
            index=[k], location=location, offset=k * chunk, length=min(chunk, size - k * chunk)
        )
        for k in range(math.ceil(size / chunk))
    ]
    if specs:
        failed = group.store.set_virtual_refs(
            array_path=array.path, chunks=specs, validate_containers=True
        )
        # Icechunk returns the indices of references it drops; the array would read as fill.
        if failed:
            raise ValueError(
                f"{len(failed)} references match no virtual chunk container, first {location}"
            )
    return array


def declare_copy(
    group: zarr.Group, path: str, files: Sequence[tuple[str, int]], *, chunk: int = 8 << 20
) -> list[zarr.Array]:
    """Declare ``files`` (location, size) as ``uint8`` arrays of virtual references.

    One file gives the array at ``path``. Several give ``path/part.<i>.parquet``.
    The group must live in a writable session whose repo has a container for each location.
    The last chunk of a file is its remainder, so the grid is rectilinear. Unlike the
    regular grid of the ``file:`` convention, reading needs ``array.rectilinear_chunks``.
    """
    with zarr.config.set({"array.rectilinear_chunks": True}):
        if len(files) == 1:
            return [_copy_array(group, path, *files[0], chunk)]
        return [
            _copy_array(group, f"{path}/part.{i}.parquet", *file, chunk)
            for i, file in enumerate(files)
        ]
