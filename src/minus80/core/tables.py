"""Iceberg tables from Parquet files, and their ``uint8`` copies in an Icechunk repo.

A :class:`TableWriter` writes each Parquet file itself, so the footer carries GeoParquet
``geo`` and pandas index metadata, and registers the files with ``add_files``.
Each column can carry the field id of the table's schema, so a reader needs no name mapping.
:func:`declare_copy` then references the same files from the repo, so a Python
reader sees the file and a reader of the ``iceberg_table`` attribute uses the table.
Each reference carries the file's last-modified time, so a changed file fails to read.

A copy's last chunk is shorter than the others, so its grid is rectilinear. A reader
must set ``zarr.config.set({"array.rectilinear_chunks": True})`` to open it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import pyarrow as pa
import pyarrow.parquet as pq
import zarr

from .progress import MIB
from .registry import file_path

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


def _with_field_id(field: pa.Field, field_ids: Mapping[str, int], prefix: str = "") -> pa.Field:
    """``field`` with ``PARQUET:field_id`` set from ``field_ids``, struct children included."""
    name = f"{prefix}{field.name}"
    if name not in field_ids:
        raise KeyError(f"column {name} has no field id")
    type_ = field.type
    if pa.types.is_struct(type_):
        type_ = pa.struct([_with_field_id(child, field_ids, f"{name}.") for child in type_])
    elif pa.types.is_list(type_) or pa.types.is_large_list(type_) or pa.types.is_fixed_size_list(type_) \
            or pa.types.is_list_view(type_) or pa.types.is_map(type_):
        raise NotImplementedError(f"column {name} is a {type_}; only struct children get field ids")
    metadata = {**(field.metadata or {}), b"PARQUET:field_id": str(field_ids[name]).encode()}
    return pa.field(field.name, type_, field.nullable, metadata)


def parquet_field_ids(schema: pa.Schema) -> dict[str, int | None]:
    """Field id by dotted column name, struct children included, as a Parquet footer states it."""
    def walk(fields, prefix=""):
        for f in fields:
            found = (f.metadata or {}).get(b"PARQUET:field_id")
            yield f"{prefix}{f.name}", None if found is None else int(found)
            if pa.types.is_struct(f.type):
                yield from walk(f.type, f"{prefix}{f.name}.")
    return dict(walk(schema))


def _check_no_nulls(table, identifier: str, name: str) -> None:
    """Raise ``ValueError`` unless every current data file states 0 nulls in column ``name``."""
    field_id = table.schema().find_field(name).field_id
    for task in table.scan().plan_files():
        count = (task.file.null_value_counts or {}).get(field_id)
        if count is None:
            raise ValueError(f"{identifier}: column {name} has no null count in {task.file.file_path}")
        if count > 0:
            raise ValueError(f"{identifier}: column {name} has {count} null values in {task.file.file_path}")


def parquet_schema(
    schema: pa.Schema,
    *,
    index_column: str,
    geo: Mapping | None = None,
    field_ids: Mapping[str, int] | None = None,
) -> pa.Schema:
    """``schema`` with the field ids and the pandas index and optional ``geo`` footer metadata of a data file.

    ``field_ids`` maps each dotted column name to its Iceberg field id, as
    ``pyiceberg.schema.index_by_name`` gives it.
    """
    if field_ids is not None:
        schema = pa.schema([_with_field_id(f, field_ids) for f in schema], schema.metadata)
    # pyarrow describes the columns from an empty frame, so no data is copied.
    frame = schema.empty_table().to_pandas()
    pandas_meta = json.loads(pa.Schema.from_pandas(frame, preserve_index=False).metadata[b"pandas"])
    pandas_meta["index_columns"] = [index_column]
    metadata = dict(schema.metadata or {})
    metadata[b"pandas"] = json.dumps(pandas_meta).encode()
    if geo is not None:
        metadata[b"geo"] = json.dumps(geo).encode()
    return schema.with_metadata(metadata)


def write_parquet(
    table: pa.Table,
    path: str,
    *,
    index_column: str,
    geo: Mapping | None = None,
    field_ids: Mapping[str, int] | None = None,
    row_group_size: int = 1_000_000,
) -> int:
    """Write ``table`` to ``path`` with the footer of :func:`parquet_schema`. Returns the file size in bytes."""
    schema = parquet_schema(table.schema, index_column=index_column, geo=geo, field_ids=field_ids)
    pq.write_table(table.cast(schema), path, row_group_size=row_group_size)
    with open(path, "rb") as f:
        return f.seek(0, 2)


class DataFile(NamedTuple):
    """A data file's location, size in bytes, and last-modified time."""

    location: str
    size: int
    last_modified: datetime


def data_file(io, location: str) -> DataFile:
    """The :class:`DataFile` at ``location``, read through the pyiceberg FileIO ``io``.

    The time is the checksum of each copy's references, so it has one-second resolution:
    Icechunk keeps whole seconds and refuses a chunk whose object is newer than its checksum.
    So the time is rounded up, and a change within that second goes undetected.
    Raises when ``io`` states no time for the file.
    """
    file = io.new_input(location)
    # pyiceberg's InputFile has no public modification time; PyArrowFile's _file_info has it.
    if not hasattr(file, "_file_info"):
        raise ValueError(f"no last-modified time for {location}: {type(io).__name__} states none")
    try:
        info = file._file_info()
    except OSError as e:
        raise type(e)(f"{location}: {e}") from e
    if info.mtime_ns is None:
        raise ValueError(f"no last-modified time for {location}: the store states none")
    seconds = -(-info.mtime_ns // 1_000_000_000)
    return DataFile(location, info.size, datetime.fromtimestamp(seconds, timezone.utc))


class TableRef(NamedTuple):
    """An Iceberg table snapshot and each registered file."""

    table_uuid: str
    snapshot_id: int
    files: list[DataFile]


def require_table(
    catalog,
    identifier: str,
    *,
    schema: pa.Schema,
    properties: Mapping[str, str],
    partition_by: str | None = None,
):
    """The table ``identifier``, created when absent and partitioned by identity on ``partition_by``.

    An existing table with another identity partition spec raises ``ValueError``; the spec is not evolved.
    A column that ``schema`` marks non-nullable becomes required in an existing table too,
    when every current data file states 0 nulls in it. Otherwise it raises ``ValueError``.
    """
    if catalog.table_exists(identifier):
        table = catalog.load_table(identifier)
        present = [table.schema().find_column_name(f.source_id) for f in table.spec().fields]
        wanted = [] if partition_by is None else [partition_by]
        if present != wanted:
            raise ValueError(f"{identifier} is partitioned by {present}, not {wanted}")
        names = {f.name for f in table.schema().fields}
        if lacking := [f.name for f in schema if not f.nullable and f.name not in names]:
            raise ValueError(f"{identifier} has no column {', '.join(lacking)}")
        loose = [f.name for f in schema if not f.nullable and not table.schema().find_field(f.name).required]
        for name in loose:
            _check_no_nulls(table, identifier, name)
        if loose:
            # pyiceberg refuses optional to required by default; the null counts above show none.
            with table.update_schema(allow_incompatible_changes=True) as update:
                for name in loose:
                    update.update_column(name, required=True)
            table = catalog.load_table(identifier)
        return table
    # One transaction commits the table and its spec together.
    with catalog.create_table_transaction(identifier, schema=schema, properties=dict(properties)) as transaction:
        if partition_by is not None:
            with transaction.update_spec() as spec:
                spec.add_identity(partition_by)
    return catalog.load_table(identifier)


def register_files(
    catalog,
    identifier: str,
    files: Sequence[str],
    *,
    schema: pa.Schema,
    properties: Mapping[str, str],
    partition_by: str | None = None,
    replace: bool = False,
) -> TableRef:
    """Add ``files`` to the table ``identifier``, creating it when absent.

    A new table is partitioned by identity on ``partition_by``. A file the current snapshot
    already holds is not added again, so a rerun returns the same snapshot. With ``replace``,
    one overwrite snapshot also removes every other file from the table; the objects stay.
    """
    from pyiceberg.io.pyarrow import parquet_file_to_data_file

    table = require_table(catalog, identifier, schema=schema, properties=properties,
                          partition_by=partition_by)
    held = {task.file.file_path: task.file for task in table.scan().plan_files()}
    new = [f for f in files if f not in held]
    stale = [held_file for path, held_file in held.items() if path not in files] if replace else []
    if stale:
        with table.transaction() as transaction:
            with transaction.update_snapshot().overwrite() as overwrite:
                for held_file in stale:
                    overwrite.delete_data_file(held_file)
                for f in new:
                    overwrite.append_data_file(parquet_file_to_data_file(table.io, transaction.table_metadata, f))
        table = catalog.load_table(identifier)
    elif new:
        table.add_files(new)
        table = catalog.load_table(identifier)
    return TableRef(
        str(table.metadata.table_uuid), table.current_snapshot().snapshot_id,
        [data_file(table.io, f) for f in files],
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


def _copy_array(group: zarr.Group, path: str, file: DataFile, chunk: int) -> zarr.Array:
    import icechunk

    location, size = file.location, file.size
    if file.last_modified is None:
        raise ValueError(f"{location} has no last-modified time, so its references would have no checksum")
    edges = [chunk] * (size // chunk) + ([size % chunk] if size % chunk else [])
    array = group.create_array(
        path, shape=(size,), chunks=[edges] if edges else (1,), dtype="uint8",
        compressors=None, filters=None, overwrite=True,
    )
    declare_file(array, array.basename)
    specs = [
        # The checksum makes Icechunk refuse the chunk once the file changes.
        icechunk.VirtualChunkSpec(
            index=[k], location=location, offset=k * chunk, length=min(chunk, size - k * chunk),
            last_updated_at_checksum=file.last_modified,
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
    group: zarr.Group, path: str, files: Sequence[DataFile], *, chunk: int = 8 << 20
) -> list[zarr.Array]:
    """Declare ``files`` as ``uint8`` arrays of virtual references.

    One file gives the array at ``path``. Several give ``path/part.<i>.parquet``.
    The group must live in a writable session whose repo has a container for each location.
    The last chunk of a file is its remainder, so the grid is rectilinear. Unlike the
    regular grid of the ``file:`` convention, reading needs ``array.rectilinear_chunks``.
    """
    with zarr.config.set({"array.rectilinear_chunks": True}):
        if len(files) == 1:
            return [_copy_array(group, path, files[0], chunk)]
        return [
            _copy_array(group, f"{path}/part.{i}.parquet", file, chunk)
            for i, file in enumerate(files)
        ]


def local_catalog(directory: Path):
    """The SQLite catalog in ``directory``, whose warehouse is ``directory`` too.

    The warehouse URL is not percent-encoded, because pyiceberg reads a ``file://`` path as it is.
    """
    from pyiceberg.catalog.sql import SqlCatalog

    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    return SqlCatalog("local", uri=f"sqlite:///{directory}/catalog.db", warehouse=f"file://{directory}")


def _content_name(path: Path) -> str:
    """The first 32 hex digits of the SHA-256 of the file at ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(16 * MIB):
            digest.update(block)
    return digest.hexdigest()[:32]


def _write_local(source: Path, url: str) -> None:
    """Copy ``source`` to the ``file://`` URL ``url``. The final name never holds a partial file."""
    path = file_path(url)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".part")
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as data:
            shutil.copyfileobj(data, target, 16 * MIB)
        # The name holds the content hash, so a concurrent writer's replace puts equal bytes.
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_size(io, source: Path, url: str) -> None:
    """Raise ``ValueError`` when the size of the object at ``url`` differs from the size of ``source``."""
    found, expected = len(io.new_input(url)), source.stat().st_size
    if found != expected:
        raise ValueError(f"{url} has size {found}, which differs from the size of the source, {expected}: it is partial, so it is not used")


def _put(io, source: Path, url: str) -> None:
    """Make the object at ``url`` hold ``source``'s bytes; a complete object stays as it is.

    The name holds a hash of the bytes. A local object is renamed into place, so it is complete.
    A remote object is written in place, so its size is checked before and after the write.
    """
    if url.startswith("file://"):
        if not file_path(url).exists():
            _write_local(source, url)
        return
    if not io.new_input(url).exists():
        with source.open("rb") as data, io.new_output(url).create(overwrite=False) as target:
            shutil.copyfileobj(data, target, 16 * MIB)
    _check_size(io, source, url)


class TableWriter:
    """Parquet files written into the Iceberg tables of one namespace, each under its table's location."""

    def __init__(self, catalog, namespace: str, *, catalog_name: str, org: str, index_column: str = "instance"):
        self.catalog, self.namespace = catalog, namespace
        self.catalog_name, self.org, self.index_column = catalog_name, org, index_column
        self._specs: dict[str, dict] = {}
        if (namespace,) not in self.catalog.list_namespaces():
            self.catalog.create_namespace(namespace)

    def _spec(self, name: str) -> dict:
        if name not in self._specs:
            raise ValueError(f"table {name} is not required; call require first")
        return self._specs[name]

    def require(self, name: str, *, schema, properties, partition_by=None):
        """The table ``name``, created when absent, with ``properties`` set; ``write`` then uses this spec."""
        self._specs[name] = {"schema": schema, "properties": properties, "partition_by": partition_by}
        table = require_table(self.catalog, f"{self.namespace}.{name}", schema=schema,
                              properties=properties, partition_by=partition_by)
        if any(table.properties.get(k) != v for k, v in properties.items()):
            with table.transaction() as transaction:
                transaction.set_properties(dict(properties))
            table = self.catalog.load_table(f"{self.namespace}.{name}")
        return table

    def field_ids(self, name: str) -> dict[str, int]:
        """The Iceberg field id of each dotted column name of the required table ``name``."""
        from pyiceberg.schema import index_by_name

        self._spec(name)
        return index_by_name(self.catalog.load_table(f"{self.namespace}.{name}").schema())

    def data_prefix(self, name: str) -> str:
        """The URL prefix that holds the Parquet files of the required table ``name``."""
        self._spec(name)
        return f"{self.catalog.load_table(f'{self.namespace}.{name}').location()}/data/"

    def write(self, name: str, table_or_path, *, geo=None, replace=False) -> TableRef:
        """Write a table, or a Parquet file at a path, as the one file of ``name``, and register it.

        A table gets the field ids of the Iceberg schema, the writer's index column and ``geo`` in the footer.
        A file at a path is used as it is, and takes no ``geo``. See :meth:`write_parts` for the rest.
        """
        if isinstance(table_or_path, Path):
            if geo is not None:
                raise ValueError("geo applies to a table; a Parquet file carries its own footer")
            return self.write_parts(name, [table_or_path], replace=replace)
        from pyiceberg.schema import index_by_name

        iceberg = self.require(name, **self._spec(name))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.parquet"
            write_parquet(table_or_path, str(path), index_column=self.index_column, geo=geo,
                          field_ids=index_by_name(iceberg.schema()))
            return self._register(iceberg, name, [path], replace)

    def _partition_files(self, name: str) -> tuple:
        """The table ``name`` and its current data files, each with its identity partition value."""
        identifier = f"{self.namespace}.{name}"
        table = self.catalog.load_table(identifier)
        if len(table.spec().fields) != 1:
            raise ValueError(f"{identifier} is not partitioned by one column")
        # The plan reads manifests only, never data files.
        return table, [(task.file.partition[0], task.file) for task in table.scan().plan_files()]

    def partitions(self, name: str) -> dict:
        """The record count of each partition value in the current snapshot of ``name``, from the manifests."""
        counts: dict = {}
        for value, file in self._partition_files(name)[1]:
            counts[value] = counts.get(value, 0) + file.record_count
        return counts

    def partition_ranges(self, name: str, column: str) -> dict:
        """The (min, max) of ``column`` in each partition value of the current snapshot, from the manifests.

        A data file with no bounds for ``column`` raises ``ValueError``.
        """
        from pyiceberg.conversions import from_bytes

        table, held = self._partition_files(name)
        field = table.schema().find_field(column)
        ranges: dict = {}
        for value, file in held:
            lower, upper = (b.get(field.field_id) for b in (file.lower_bounds or {}, file.upper_bounds or {}))
            if lower is None or upper is None:
                raise ValueError(f"{file.file_path} has no bounds for column {column}")
            lo, hi = from_bytes(field.field_type, lower), from_bytes(field.field_type, upper)
            old = ranges.get(value, (lo, hi))
            ranges[value] = (min(old[0], lo), max(old[1], hi))
        return ranges

    def ref(self, name: str, *, partition_order: Sequence | None = None) -> TableRef:
        """The current snapshot of ``name`` with all its files, ordered by ``partition_order`` when given.

        Files of one partition value stay in manifest order. A value not in ``partition_order`` raises ``ValueError``.
        """
        table, held = self._partition_files(name)
        if table.current_snapshot() is None:
            raise ValueError(f"{self.namespace}.{name} has no snapshot")
        if partition_order is not None:
            rank = {value: i for i, value in enumerate(partition_order)}
            if missing := sorted({str(v) for v, _ in held if v not in rank}):
                raise ValueError(f"{self.namespace}.{name} holds partitions outside the order: {', '.join(missing)}")
            held.sort(key=lambda item: rank[item[0]])
        return TableRef(
            str(table.metadata.table_uuid), table.current_snapshot().snapshot_id,
            [data_file(table.io, file.file_path) for _, file in held],
        )

    def write_parts(self, name: str, paths: Sequence[Path], *, replace=False) -> TableRef:
        """Write each Parquet file of ``paths`` into the table ``name``, and register all in one snapshot.

        Without ``replace`` the files are added to the table and its earlier files stay.
        The returned :class:`TableRef` lists only ``paths``' files; :meth:`ref` lists all of the table's.

        A file name holds a hash of the file's bytes, so other bytes never take an existing name.
        A complete object under that name is reused. A remote object of another size raises ``ValueError``.
        A local file goes to a unique ``.part`` name first, then replaces the final name; a crash leaves that file, which no later write uses.
        With ``replace``, the files replace every file the table holds. A failure after the table
        snapshot and before the repo commit leaves one extra snapshot; the next run converges.
        """
        return self._register(self.require(name, **self._spec(name)), name, paths, replace)

    def _register(self, iceberg, name: str, paths: Sequence[Path], replace: bool) -> TableRef:
        urls = []
        for path in paths:
            url = f"{iceberg.location()}/data/{name}-{_content_name(path)}.parquet"
            _put(iceberg.io, path, url)
            urls.append(url)
        return register_files(self.catalog, f"{self.namespace}.{name}", urls, replace=replace, **self._spec(name))
