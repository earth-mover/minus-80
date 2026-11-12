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
from pyarrow.fs import FileType

from .progress import MIB
from .registry import file_path

#: The bytes of one virtual reference in a ``uint8`` copy of a data file.
COPY_CHUNK = 8 << 20

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
    # Only a PyArrowFileIO has a public filesystem to ask; the FileIO and InputFile APIs state no time.
    if not hasattr(io, "fs_by_scheme"):
        raise ValueError(f"no last-modified time for {location}: {type(io).__name__} states none")
    scheme, netloc, path = io.parse_location(location, io.properties)
    try:
        info = io.fs_by_scheme(scheme, netloc).get_file_info(path)
    except OSError as e:
        raise type(e)(f"{location}: {e}") from e
    if info.type == FileType.NotFound:
        raise FileNotFoundError(f"{location}: file not found")
    if info.mtime_ns is None:
        raise ValueError(f"no last-modified time for {location}: the store states none")
    seconds = -(-info.mtime_ns // 1_000_000_000)
    return DataFile(location, info.size, datetime.fromtimestamp(seconds, timezone.utc))


class HeldFilesError(ValueError):
    """A write without ``replace`` met a live data file of other content in the table ``table``."""

    def __init__(self, message: str, table: str):
        super().__init__(message)
        self.table = table


class TableRef(NamedTuple):
    """An Iceberg table snapshot and each registered file."""

    table_uuid: str
    snapshot_id: int
    files: list[DataFile]


def _plain(field: pa.Field) -> pa.Field:
    """``field`` with each dictionary type as its value type, the type that Iceberg has; pyiceberg logs a warning otherwise."""
    type_ = field.type
    if pa.types.is_dictionary(type_):
        type_ = type_.value_type
    elif pa.types.is_struct(type_):
        type_ = pa.struct([_plain(child) for child in type_])
    return pa.field(field.name, type_, field.nullable)


def _same_type(held, wanted) -> bool:
    """True when two Iceberg types are equal at every level, ignoring field ids and nullability."""
    from pyiceberg.types import ListType, MapType, StructType

    if isinstance(held, StructType) and isinstance(wanted, StructType):
        return [f.name for f in held.fields] == [f.name for f in wanted.fields] and all(
            _same_type(a.field_type, b.field_type) for a, b in zip(held.fields, wanted.fields))
    if isinstance(held, ListType) and isinstance(wanted, ListType):
        return _same_type(held.element_type, wanted.element_type)
    if isinstance(held, MapType) and isinstance(wanted, MapType):
        return _same_type(held.key_type, wanted.key_type) and _same_type(held.value_type, wanted.value_type)
    return held == wanted


def _type_text(type_) -> str:
    """An Iceberg type as text, without field ids."""
    from pyiceberg.types import StructType

    if isinstance(type_, StructType):
        return f"struct<{', '.join(f'{f.name}: {_type_text(f.field_type)}' for f in type_.fields)}>"
    return str(type_)


def schema_changes(table, schema: pa.Schema) -> tuple[list[str], list[str]]:
    """The columns that ``schema`` adds to ``table`` and the columns it deletes, each in schema order.

    A kept column of another type raises ``ValueError`` with both types.
    Types compare without field ids and without nullability, at every level.
    """
    from pyiceberg.io.pyarrow import pyarrow_to_schema
    from pyiceberg.table.name_mapping import create_mapping_from_schema

    present = table.schema()
    names, wanted_names = {f.name for f in present.fields}, {f.name for f in schema}
    mapping = create_mapping_from_schema(present)
    for field in (f for f in schema if f.name in names):
        held = present.find_field(field.name).field_type
        try:
            # The table's ids go to the wanted column by name; an unmapped nested name is a type difference.
            found = pyarrow_to_schema(pa.schema([_plain(field)]), name_mapping=mapping).fields[0].field_type
        except ValueError:
            found = None
        if found is None or not _same_type(held, found):
            raise ValueError(f"{'.'.join(table.name())}: column {field.name} is {_type_text(held)}, "
                             f"the wanted schema has {field.type if found is None else _type_text(found)}")
    return ([f.name for f in schema if f.name not in names],
            [f.name for f in present.fields if f.name not in wanted_names])


def require_table(
    catalog,
    identifier: str,
    *,
    schema: pa.Schema,
    properties: Mapping[str, str],
    partition_by: str | None = None,
    evolve: bool = False,
):
    """The table ``identifier``, created when absent and partitioned by identity on ``partition_by``.

    An existing table with another identity partition spec raises ``ValueError``; the spec is not evolved.
    A column that ``schema`` marks non-nullable becomes required in an existing table too,
    when every current data file states 0 nulls in it. Otherwise it raises ``ValueError``.
    An existing table whose columns differ from ``schema`` raises ``ValueError``, unless ``evolve``:
    then one schema commit adds each missing column, as optional with a new field id, and deletes each other column.
    A kept column of another type raises ``ValueError`` in both cases.
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
        added, deleted = schema_changes(table, schema)
        if (added or deleted) and not evolve:
            raise ValueError(f"{identifier} differs from the wanted schema, which adds {', '.join(added) or 'no column'} "
                             f"and deletes {', '.join(deleted) or 'no column'}")
        if added or deleted:
            with table.update_schema() as update:
                for name in deleted:
                    update.delete_column(name)
                # Only the missing columns go in, so no kept column changes type or required flag.
                update.union_by_name(pa.schema([_plain(f) for f in schema if f.name in added]))
            table = catalog.load_table(identifier)
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
    with catalog.create_table_transaction(identifier, schema=pa.schema([_plain(f) for f in schema]), properties=dict(properties)) as transaction:
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
    replace_partitions: Sequence = (),
) -> TableRef:
    """Add ``files`` to the table ``identifier``, creating it when absent.

    A new table is partitioned by identity on ``partition_by``. A file the current snapshot
    already holds is not added again, so a rerun returns the same snapshot. With ``replace``,
    one overwrite snapshot also removes every other file from the table; the objects stay.
    ``replace_partitions`` removes, in the same snapshot, the other files of those partition values.
    """
    from pyiceberg.io.pyarrow import parquet_file_to_data_file

    table = require_table(catalog, identifier, schema=schema, properties=properties,
                          partition_by=partition_by)
    held = {task.file.file_path: task.file for task in table.scan().plan_files()}
    new = [f for f in files if f not in held]
    stale = [held_file for path, held_file in held.items()
             if path not in files and (replace or (replace_partitions and held_file.partition[0] in replace_partitions))]
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


def copy_edges(size: int, chunk: int = COPY_CHUNK) -> list[int]:
    """The chunk edges of a copy of a ``size``-byte file: whole chunks, then the remainder."""
    return [chunk] * (size // chunk) + ([size % chunk] if size % chunk else [])


def _copy_array(group: zarr.Group, path: str, file: DataFile, chunk: int) -> zarr.Array:
    import icechunk

    location, size = file.location, file.size
    if file.last_modified is None:
        raise ValueError(f"{location} has no last-modified time, so its references would have no checksum")
    edges = copy_edges(size, chunk)
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
    group: zarr.Group, path: str, files: Sequence[DataFile], *, chunk: int = COPY_CHUNK
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


#: Attempts of one upload. A vended credential can expire during a long upload, and pyarrow signs each part with it.
#: The catalog keeps a credential for most of its life, so a load after the failure gets a new one.
UPLOAD_ATTEMPTS = 3


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


def partition_files(table, snapshot_id: int | None = None) -> list:
    """The data files of ``table`` at ``snapshot_id``, or the current snapshot, each with its identity partition value.

    The plan reads manifests only, never data files.
    """
    if len(table.spec().fields) != 1:
        raise ValueError(f"{'.'.join(table.name())} is not partitioned by one column")
    return [(task.file.partition[0], task.file) for task in table.scan(snapshot_id=snapshot_id).plan_files()]


def partition_counts(files: Sequence) -> dict:
    """The record count of each partition value of ``files``, as :func:`partition_files` gives them."""
    counts: dict = {}
    for value, file in files:
        counts[value] = counts.get(value, 0) + file.record_count
    return counts


def snapshot_schema(table, snapshot_id: int | None = None):
    """The schema that ``table`` had at ``snapshot_id``, or its current schema without one."""
    if snapshot_id is None:
        return table.schema()
    snapshot = table.snapshot_by_id(snapshot_id)
    if snapshot is None:
        raise ValueError(f"{'.'.join(table.name())} has no snapshot {snapshot_id}")
    return table.schema() if snapshot.schema_id is None else table.schemas()[snapshot.schema_id]


def partition_bounds(table, files: Sequence, column: str, snapshot_id: int | None = None) -> dict:
    """The (min, max) of ``column`` in each partition value of ``files``, as :func:`partition_files` gives them.

    ``column`` is looked up in the schema of ``snapshot_id``, or the current schema without one.
    A data file with no bounds for ``column`` raises ``ValueError``.
    """
    from pyiceberg.conversions import from_bytes

    field = snapshot_schema(table, snapshot_id).find_field(column)
    ranges: dict = {}
    for value, file in files:
        lower, upper = (b.get(field.field_id) for b in (file.lower_bounds or {}, file.upper_bounds or {}))
        if lower is None or upper is None:
            raise ValueError(f"{file.file_path.rsplit('/', 1)[-1]} has no bounds for column {column}")
        lo, hi = from_bytes(field.field_type, lower), from_bytes(field.field_type, upper)
        old = ranges.get(value, (lo, hi))
        ranges[value] = (min(old[0], lo), max(old[1], hi))
    return ranges


def row_group_bounds(stream, column: str) -> list[tuple]:
    """The ``(min, max, rows)`` of ``column`` in each row group of a Parquet file, from the footer only.

    ``KeyError`` names an absent column. A row group without statistics raises ``ValueError``.
    """
    metadata = pq.ParquetFile(stream).metadata
    index = next((i for i in range(metadata.num_columns) if metadata.schema.column(i).path == column), None)
    if index is None:
        raise KeyError(column)
    found = []
    for n in range(metadata.num_row_groups):
        group = metadata.row_group(n)
        stats = group.column(index).statistics
        if stats is None or not stats.has_min_max:
            raise ValueError(f"row group {n} has no statistics for column {column}")
        found.append((stats.min, stats.max, group.num_rows))
    return found


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

    def require(self, name: str, *, schema, properties, partition_by=None, evolve=False):
        """The table ``name``, created when absent, with ``properties`` set; ``write`` then uses this spec.

        ``evolve`` changes an existing table's columns to ``schema``; see :func:`require_table`.
        """
        self._specs[name] = {"schema": schema, "properties": properties, "partition_by": partition_by}
        table = require_table(self.catalog, f"{self.namespace}.{name}", schema=schema,
                              properties=properties, partition_by=partition_by, evolve=evolve)
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

    def write(self, name: str, table_or_path, *, geo=None, replace=False, index_column: str | None = None) -> TableRef:
        """Write a table, or a Parquet file at a path, as the one file of ``name``, and register it.

        A table gets the field ids of the Iceberg schema, ``index_column`` (default: the writer's) and ``geo`` in the footer.
        A file at a path is used as it is, and takes no ``geo``. See :meth:`write_parts` for the rest.
        Without ``replace``, a table that holds a live file of other content raises ``ValueError``; an equal write is a no-op.
        """
        if isinstance(table_or_path, Path):
            if geo is not None:
                raise ValueError("geo applies to a table; a Parquet file carries its own footer")
            return self._register(self.require(name, **self._spec(name)), name, [table_or_path], replace, exclusive=True)
        from pyiceberg.schema import index_by_name

        iceberg = self.require(name, **self._spec(name))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.parquet"
            write_parquet(table_or_path, str(path), index_column=index_column or self.index_column, geo=geo,
                          field_ids=index_by_name(iceberg.schema()))
            return self._register(iceberg, name, [path], replace, exclusive=True)

    def _partition_files(self, name: str) -> tuple:
        """The table ``name`` and its current data files, each with its identity partition value."""
        table = self.catalog.load_table(f"{self.namespace}.{name}")
        return table, partition_files(table)

    def partitions(self, name: str) -> dict:
        """The record count of each partition value in the current snapshot of ``name``, from the manifests."""
        return partition_counts(self._partition_files(name)[1])

    def partition_ranges(self, name: str, column: str) -> dict:
        """The (min, max) of ``column`` in each partition value of the current snapshot, from the manifests.

        A data file with no bounds for ``column`` raises ``ValueError``.
        """
        table, held = self._partition_files(name)
        return partition_bounds(table, held, column)

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

    def remove_partitions(self, name: str, values: Sequence) -> TableRef:
        """Delete the data files of the partition ``values`` from the current snapshot of ``name``.

        The objects stay, and earlier snapshots list them. No file to delete commits no snapshot.
        """
        return self.write_parts(name, [], replace_partitions=values)

    def write_parts(self, name: str, paths: Sequence[Path], *, replace=False, replace_partitions: Sequence = ()) -> TableRef:
        """Write each Parquet file of ``paths`` into the table ``name``, and register all in one snapshot.

        Without ``replace`` the files are added to the table and its earlier files stay.
        The returned :class:`TableRef` lists only ``paths``' files; :meth:`ref` lists all of the table's.

        A file name holds a hash of the file's bytes, so other bytes never take an existing name.
        A complete object under that name is reused. A remote object of another size raises ``ValueError``.
        A local file goes to a unique ``.part`` name first, then replaces the final name; a crash leaves that file, which no later write uses.
        With ``replace``, the files replace every file the table holds. A failure after the table
        snapshot and before the repo commit leaves one extra snapshot; the next run converges.
        ``replace_partitions`` replaces the files of those partition values, in the same snapshot.
        A table of several files is the caller's to keep right: :meth:`write` refuses a second file, this does not.
        """
        return self._register(self.require(name, **self._spec(name)), name, paths, replace, replace_partitions)

    def _upload(self, iceberg, name: str, path: Path, url: str) -> None:
        """:func:`_put` ``path`` at ``url``; an ``OSError`` loads the table again and repeats, up to :data:`UPLOAD_ATTEMPTS`."""
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            try:
                return _put(iceberg.io, path, url)
            except OSError as error:
                if attempt == UPLOAD_ATTEMPTS:
                    raise
                # The text of the error can hold a storage location, so only its type shows.
                print(f"{name}: an upload failed ({type(error).__name__}); attempt {attempt + 1} of {UPLOAD_ATTEMPTS} "
                      "loads the table again for new credentials", flush=True)
                iceberg = self.catalog.load_table(f"{self.namespace}.{name}")

    def _register(self, iceberg, name: str, paths: Sequence[Path], replace: bool, replace_partitions: Sequence = (),
                  *, exclusive: bool = False) -> TableRef:
        urls = [f"{iceberg.location()}/data/{name}-{_content_name(path)}.parquet" for path in paths]
        if exclusive and not replace:
            # One write is the whole table, so another live file would mix two writes' rows.
            other = [t.file.file_path for t in iceberg.scan().plan_files() if t.file.file_path not in urls]
            if other:
                raise HeldFilesError(f"{self.namespace}.{name} holds {len(other)} data file{'s' * (len(other) != 1)} "
                                     "of other content", name)
        for path, url in zip(paths, urls):
            self._upload(iceberg, name, path, url)
        return register_files(self.catalog, f"{self.namespace}.{name}", urls, replace=replace,
                              replace_partitions=replace_partitions, **self._spec(name))
