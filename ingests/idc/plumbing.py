"""The plumbing under ``ingest.py``: IDC's bucket, the writes into a session, and the threads.

``ingest.py`` holds what IDC publishes and how the store holds it. This module
holds what its steps need and none of them is about:

- :class:`Source`, the bucket (or a test server laid out like it): each
  series' objects with their sizes and ETags, and a seekable reader per object;
- :class:`Writer`, which writes groups, pinned virtual arrays, native arrays
  and documents into one Icechunk session;
- :func:`open_repository`, the repo at a local path or in Arraylake;
- :func:`in_forks`, which runs one job per slide in a pool of processes, each
  in its own fork of a session, and returns the forks to merge.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field

import numpy as np
import obstore
import obstore.exceptions
import zarr
from minus80.core import remote
from minus80.core.fetch import download_then_index
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo, write_pinned
from virtualizarr.manifests import ManifestArray, ManifestGroup

#: IDC's public AWS bucket (us-east-1, anonymous). Every series is a prefix
#: named by its ``crdc_series_uuid``, holding one object per instance.
BUCKET = "idc-open-data"
SOURCE = f"s3://{BUCKET}/"

os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")

BLOSC = zarr.codecs.BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")
ZSTD = zarr.codecs.ZstdCodec(level=5)


def say(line: str) -> None:
    """Print one line at once, with the time, so a remote run's log shows where it is."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


@dataclass
class Source:
    """IDC's objects under ``root``: the bucket, or an HTTP server laid out like it.

    An HTTP server cannot be listed, so a test passes ``listing``: each
    series prefix's ``(name, size, etag)`` triples.
    """

    root: str = SOURCE
    listing: dict[str, list[tuple[str, int, str]]] | None = field(default=None)

    def objects(self, series: str) -> list[tuple[str, int, str]]:
        """``(url, size, etag)`` of every DICOM object in one series' prefix, from one listing."""
        if self.listing is not None:
            return [(f"{self.root}{series}/{n}", size, etag) for n, size, etag in self.listing[series]]
        from obstore.store import S3Store

        store = S3Store(BUCKET, region="us-east-1", skip_signature=True)
        return [(f"{self.root}{o['path']}", int(o["size"]), o["e_tag"])
                for batch in obstore.list(store, prefix=f"{series}/")
                for o in batch if o["path"].endswith(".dcm")]

    def reader(self, url: str):
        """A seekable file over ``url`` that fetches only the blocks a parser reads."""
        return remote.reader(url)


def local(url: str) -> str:
    """A local copy of ``url``, fetched once with parallel range GETs."""
    return download_then_index(url)


class Writer:
    """Writes groups, arrays and documents into one Icechunk session."""

    def __init__(self, session):
        self.session = session
        self.root = zarr.open_group(session.store, mode="a")

    def group(self, path: str, attrs: dict | None = None) -> zarr.Group:
        group = self.root.require_group(path) if path else self.root
        if attrs:
            group.attrs.update(attrs)
        return group

    def virtual(self, path: str, arrays: dict[str, ManifestArray], attrs: dict, etags: dict[str, str]) -> None:
        """A group of virtual arrays whose references are pinned to their objects' ETags."""
        self.group(path.rsplit("/", 1)[0])
        write_pinned(self.session.store, path, ManifestGroup(arrays=arrays, attributes=attrs), etags)

    def native(self, path: str, data: np.ndarray, *, dims, chunks=None, attrs=None,
               compressors=ZSTD) -> zarr.Array:
        parent, name = path.rsplit("/", 1)
        array = self.group(parent).create_array(
            name, shape=data.shape, chunks=chunks or data.shape or (1,), dtype=data.dtype,
            dimension_names=dims, attributes=attrs or {}, compressors=compressors, fill_value=0,
            config={"write_empty_chunks": False})
        if data.size:
            array[:] = data
        return array

    def document(self, path: str, value: dict) -> dict:
        """``value`` as UTF-8 JSON in a native byte array, and the pointer to it."""
        data = np.frombuffer(json.dumps(value).encode(), np.uint8)
        self.native(path, data, dims=("byte",), attrs={"format": "JSON, UTF-8"})
        return {"array": path, "bytes": int(data.size), "format": "JSON, UTF-8"}


def open_repository(location: str, root: str = SOURCE):
    """The repo at a local path or ``arraylake://org/repo``, a local one created on first use.

    ``root`` is the prefix its virtual chunks may read: the bucket, or a
    test server.
    """
    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[root])
    return create_repo(location, [], prefixes=[root], open_existing=True)


#: Tries per slide. A run over 25,000 slides can outlast an S3 outage longer
#: than obstore's own retries.
TRIES = 3
RETRY_AFTER = 30.0

#: The errors a read can raise that a later try may not: the network's and
#: object stores'. Any other error is the same on every try.
TRANSIENT = (OSError, obstore.exceptions.BaseError)


def in_forks(session, units: Mapping[str, object], job: Callable, *, workers: int,
             progress_every: float = 30.0) -> tuple[dict, dict[str, str]]:
    """Run ``job(fork, units[name])`` for every unit in a pool of processes, each in its own fork.

    Returns ``{unit: (fork, result)}`` for the units that succeeded and
    ``{unit: reason}`` for those that failed. A unit whose read fails with a
    :data:`TRANSIENT` error is tried again, up to :data:`TRIES` times, in a
    fresh fork; any other error fails it at once. The caller merges the forks
    into ``session`` and commits once: many sessions committing to one branch
    would each rebase onto the others (docs/ingest.md).

    Processes, not threads: a GTEx slide costs about 0.2 s of CPU in pydicom's
    pure-Python header parse, so threads queue on the GIL and one process
    tops out near five slides a second however many reads are in flight. A
    fork pickles with its changes, so it travels to a worker and back. The
    pool is spawned, because forking a process after obstore has started its
    threads can deadlock (docs/formats.md). ``job`` must be importable from
    its module. A line is printed every ``progress_every`` seconds, so a hung
    read shows as a count that stops moving.
    """
    started = last = time.monotonic()
    done: dict = {}
    failed: dict[str, str] = {}
    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(attempt, job, session.fork(), name, unit): name for name, unit in units.items()}
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=progress_every, return_when=FIRST_COMPLETED)
            for future in finished:
                unit = futures[future]
                try:
                    done[unit] = future.result()
                except Exception as error:  # noqa: BLE001 -- reported with the others, nothing committed
                    failed[unit] = f"{type(error).__name__}: {error}"
            now = time.monotonic()
            if now - last >= progress_every or not pending:
                last = now
                count = len(done) + len(failed)
                rate = count / (now - started)
                left = f", about {(len(units) - count) / rate:.0f} s left" if rate else ""
                say(f"{count} of {len(units)} slides read ({len(failed)} failed) in {now - started:.0f} s, "
                    f"{rate:.1f} per s{left}")
    return done, failed


def attempt(job: Callable, fork, name: str, unit):
    """``job(fork, unit)`` in a worker process, tried again on a transient error."""
    for tries in range(1, TRIES + 1):
        try:
            return fork, job(fork, unit)
        except TRANSIENT as error:
            if tries == TRIES:
                raise
            say(f"{name}: {type(error).__name__}: {error}; try {tries + 1} of {TRIES} in {RETRY_AFTER:.0f} s")
            time.sleep(RETRY_AFTER)
