"""The plumbing under the CHAMMI-75 ingest: reading the bucket and opening the repo.

``ingest.py`` holds the pipeline and the parsing of the source: which studies
go in, what an image is, how stacks and cells are built. This module holds
what moves bytes and sets up the places they go:

- one object store for the whole archive, so a key is a ``storage.path`` value;
- listing a study's PNGs and reading their first bytes many at a time;
- the metadata CSVs, downloaded once and kept as Parquet in the work directory;
- the guidance zip's members, from its central directory, and streaming them;
- the repo, the branch, and the progress lines.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import quote

import duckdb
import obstore
import pyarrow as pa
import pyarrow.parquet as pq

from minus80.core.fetch import download_then_index
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo
from minus80.core.registry import file_path, store_and_key
from minus80.parsers.zip.directory import inflate_member, member_spans
from minus80.parsers.zip.reading import cached_central_directory, stream_members

#: Where the archive lives. Every path the metadata states is relative to it.
ARCHIVE = "s3://chammi-data/CHAMMI-75/"
TRAIN_PREFIX = "CHAMMI-75_train/"
GUIDANCE_PREFIX = "CHAMMI-75_guidance/"
METADATA_CSV = "CHAMMI-75_train_metadata.csv"
SMALL_CSV = "CHAMMI-75_small_metadata.csv"
GUIDANCE_ZIP = "CHAMMI-75_guidance.zip"

#: A PNG's signature and IHDR chunk fill its first 33 bytes, which state the
#: image's width, height, bit depth and color type.
PNG_HEADER_BYTES = 33
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: Header reads in flight at once, per study. The archive's S3 bucket answered
#: 7,577 header reads a second at 1,024 in flight from one VM in us-west-2;
#: several studies run at once, so each takes a share of that.
HEADER_CONCURRENCY = 256


#: Range requests for a study's run of the guidance zip: their size, and how
#: many are in flight. ``stream_members`` asks for up to twice as many ahead
#: as are in flight, so each study holds at most 512 MiB, and eight studies
#: at once fit in a VM's memory beside the driver's tables. A study's run is
#: up to 4.7 GB (jump0001), 23.8 GB for every allowlisted study.
MEMBER_RANGE_BYTES = 16 << 20
MEMBER_CONCURRENCY = 16


def say(line: str) -> None:
    """Print one progress line at once, with the time, so a remote run's log shows where it is."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def archive_store(archive: str):
    """One obstore store rooted at the archive, so a key is a ``storage.path`` value.

    ``store_and_key`` keeps one store per directory, and the PNGs sit in
    thousands of directories: resolving each PNG on its own would open as many
    connection pools. Resolving a file at the archive's root gives the store
    for the root, and every key below it.
    """
    store, _ = store_and_key(archive + METADATA_CSV)
    return store


def png_url(archive: str, key: str) -> str:
    """The URL of a PNG from its key, percent-encoded once, as the manifest records it."""
    return archive + quote(key, safe="/")


def list_pngs(store, study: str) -> pa.Table:
    """Every object under the study's training prefix: ``key`` and ``size`` in bytes."""
    keys, sizes = [], []
    for batch in obstore.list(store, f"{TRAIN_PREFIX}{study}/", chunk_size=1000):
        for item in batch:
            keys.append(item["path"])
            sizes.append(item["size"])
    return pa.table({"key": keys, "size": pa.array(sizes, pa.int64())})


def read_png_headers(store, keys: list[str], concurrency: int = HEADER_CONCURRENCY) -> list[bytes]:
    """The first :data:`PNG_HEADER_BYTES` of each key, in order, ``concurrency`` reads at a time.

    A failed read raises: an image whose size is unknown cannot be placed in
    a stack, so the study stops rather than drop it.
    """
    async def read_all():
        gate = asyncio.Semaphore(concurrency)

        async def one(key):
            async with gate:
                return bytes(await obstore.get_range_async(store, key, start=0, length=PNG_HEADER_BYTES))

        return await asyncio.gather(*(one(k) for k in keys))

    return asyncio.run(read_all())


def metadata_parquet(archive: str, work: Path, name: str) -> Path:
    """The metadata CSV ``name`` as Parquet in ``work``, every column as text.

    Text, because several columns mix numbers with words (``10; 40``, ``EM``,
    ``z45``) and a parse that guessed types would rewrite what the source
    states. The training CSV is 3.0 GB and its Parquet copy 62 MB, so it is
    converted once and the copy reused.
    """
    out = work / f"{Path(name).stem}.parquet"
    if out.exists():
        return out
    work.mkdir(parents=True, exist_ok=True)
    url = archive + name
    local = file_path(url) or Path(download_then_index(url, str(work / "downloads")))
    say(f"converting {name} to Parquet")
    con = duckdb.connect()
    partial = out.with_suffix(".parquet.part")
    con.execute(f"copy (select * from read_csv('{local}', header=true, all_varchar=true)) "
                f"to '{partial}' (format parquet, compression zstd)")
    partial.rename(out)
    return out


def guidance_members(archive: str, work: Path) -> Path:
    """Every member of the guidance zip with its span, kept as Parquet in ``work``.

    The central directory is read once (``cached_central_directory``, 1.49 GB
    for 7.7 million members). Each member's span runs from its local header to
    the next member's, so it can only be computed against the whole
    directory; a study's worker then needs only its own rows.
    """
    out = work / "guidance-members.parquet"
    if out.exists():
        return out
    url = archive + GUIDANCE_ZIP
    directory = cached_central_directory(url, work / "guidance-directory.parquet")
    spans = member_spans(directory)
    entries = directory.take(spans["entry"]).select(["compressed", "method", "crc32"])
    table = pa.table({"name": spans["name"], "start": spans["start"], "end": spans["end"],
                      **{c: entries[c] for c in entries.column_names}})
    pq.write_table(table, out)
    return out


def study_members(members: Path, study: str) -> list[dict]:
    """The guidance zip's members under the study's prefix, in file order."""
    prefix = f"{GUIDANCE_PREFIX}{study}/"
    return duckdb.sql(f"select * from read_parquet('{members}') "
                      f"where starts_with(name, '{prefix}') order by start").to_arrow_table().to_pylist()


def read_members(archive: str, members: list[dict], wanted: set[str]):
    """``(name, contents)`` of each member in ``wanted``, streaming every member in ``members``.

    A study's members lie together in the zip, all of an image's channel
    files beside each other. Streaming the whole run reads a few large ranges,
    where fetching the one file per image that is wanted would make a request
    per image. Each member's CRC is checked as it is inflated.
    """
    for member in stream_members(archive + GUIDANCE_ZIP, members, range_bytes=MEMBER_RANGE_BYTES,
                                 concurrency=MEMBER_CONCURRENCY, progress=False):
        if member.name in wanted:
            yield member.name, inflate_member(member.stored, member.entry)


def container_for(archive: str) -> str:
    """The virtual chunk container for an archive: its bucket, or for a local copy its directory."""
    if archive.startswith("s3://"):
        return "s3://" + archive.removeprefix("s3://").split("/", 1)[0] + "/"
    return archive


def open_for_writing(location: str, branch: str, archive: str = ARCHIVE):
    """The repo at ``location``, with ``branch`` created from ``main``'s tip if absent.

    ``location`` is a directory, an ``s3://`` prefix or
    ``arraylake://<org>/<repo>``. A local or S3 repo is created the first
    time, with the archive's bucket as its one virtual chunk container, so
    one container covers every study. An Arraylake repo must already exist,
    with the container configured on Arraylake.
    """
    container = container_for(archive)
    if location.startswith(ARRAYLAKE):
        repo = arraylake_repository(location, source_prefixes=[container])
    else:
        repo = create_repo(location, [], prefixes=[container], open_existing=True)
    if branch not in repo.list_branches():
        repo.create_branch(branch, repo.lookup_branch("main"))
    return repo
