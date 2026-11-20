"""The plumbing under ``ingest.py``: the source's bytes, the writes into a session, and the threads.

``ingest.py`` holds what the CAMELYON archive publishes and how the store
holds it. This module holds what any of its steps needs and none of them is
about:

- :class:`Source`, the bucket (or a local copy of its layout) and its keys;
- :class:`Writer`, which writes groups, virtual and native arrays and long
  documents into one Icechunk session;
- :func:`open_repository`, the repo at a local path or in Arraylake;
- :func:`in_forks`, which runs one job per slide on a thread pool, each in its
  own fork of a session, and returns the forks to merge.
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

import numpy as np
import obstore
import obstore.exceptions
import tifffile
import zarr
from minus80.core import remote
from minus80.core.documents import text_or_pointer
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo
from minus80.core.registry import store_and_key
from virtualizarr.manifests import ManifestArray, ManifestGroup

#: The public bucket both challenges live in (us-west-2, anonymous, CC0).
SOURCE = "s3://camelyon-dataset/"

# The bucket allows anonymous reads only; a signed request from a machine with
# AWS credentials for another account is refused.
os.environ.setdefault("MINUS80_S3_ANONYMOUS", "1")

ZSTD = zarr.codecs.ZstdCodec(level=5)


def say(line: str) -> None:
    """Print one line at once, with the time, so a remote run's log shows where it is."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


@dataclass(frozen=True)
class Source:
    """The archive's files under ``root``: the bucket, or a local directory laid out like it.

    Keys are paths below ``root``, such as ``CAMELYON16/images/tumor_056.tif``.
    """

    root: str = SOURCE

    def url(self, key: str) -> str:
        return self.root + key

    def read(self, key: str) -> bytes:
        """A small file's bytes, fetched once per process."""
        return read_object(self.url(key))

    def head(self, key: str) -> dict | None:
        """The object's ``size`` and ``e_tag``, or None if there is no such object."""
        store, name = store_and_key(self.url(key))
        try:
            return obstore.head(store, name)
        except FileNotFoundError:
            return None

    def keys(self, prefix: str) -> list[str]:
        """Every key under the directory ``prefix``, which ends with ``/``."""
        store, _ = store_and_key(self.url(prefix + "listing"))
        return sorted(prefix + item["path"] for batch in obstore.list(store) for item in batch)

    def open_tiff(self, key: str) -> tifffile.TiffFile:
        """The TIFF open in tifffile over block-cached range reads, so only its headers are fetched."""
        return tifffile.TiffFile(remote.reader(self.url(key)))


@functools.cache
def read_object(url: str) -> bytes:
    store, name = store_and_key(url)
    return obstore.get(store, name).bytes().to_bytes()


class Writer:
    """Writes groups, arrays and verbatim documents into one Icechunk session."""

    def __init__(self, session):
        self.session = session
        self.root = zarr.open_group(session.store, mode="a")

    def group(self, path: str, attrs: dict | None = None) -> zarr.Group:
        """The group at ``path``, created with its parents if missing, with ``attrs`` added."""
        group = self.root.require_group(path) if path else self.root
        if attrs:
            group.attrs.update(attrs)
        return group

    def text(self, parent: str, name: str, text: str, *, of: str, fmt: str):
        """``text`` itself when small, else a pointer to a byte array under ``parent/documents``."""
        return text_or_pointer(self.root, parent, name, text, of=of, fmt=fmt)

    def virtual(self, path: str, arrays: dict[str, ManifestArray], attrs: dict) -> None:
        self.group(path.rsplit("/", 1)[0])
        ManifestGroup(arrays=arrays, attributes=attrs).to_icechunk(self.session.store, group=path)

    def native(self, path: str, data: np.ndarray, *, dims, chunks=None, attrs=None) -> None:
        parent, name = path.rsplit("/", 1)
        kwargs = {"compressors": ZSTD, "fill_value": 0} if data.dtype.kind in "iuf" else {}
        dtype = str if data.dtype.kind == "T" else data.dtype
        array = self.group(parent).create_array(name, shape=data.shape, chunks=chunks or data.shape or (1,),
                                                dtype=dtype, dimension_names=dims, attributes=attrs or {},
                                                **kwargs)
        if data.size:
            array[:] = data


def open_repository(location: str, source: Source):
    """The repo at a local path or ``arraylake://org/repo``, a local one created on first use.

    Arraylake authorizes virtual chunks by the containers an org admin set up
    for the repo; a local repo gets one container for the whole source root,
    so both challenges' chunks match it.
    """
    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[source.root])
    return create_repo(location, [], prefixes=[source.root], open_existing=True)


#: Tries per slide. A run over a thousand slides can outlast an S3 outage
#: longer than obstore's own retries.
TRIES = 3
RETRY_AFTER = 30.0

#: The errors a read can raise that a later try may not: the network's and
#: object stores'. Any other error is the same on every try.
TRANSIENT = (OSError, obstore.exceptions.BaseError)


def in_forks(session, units: Sequence[str], job: Callable, *, threads: int,
             progress_every: float = 30.0) -> tuple[dict, dict[str, str]]:
    """Run ``job(fork, unit)`` for every unit, each in its own fork of ``session``.

    Returns ``{unit: (fork, result)}`` for the units that succeeded and
    ``{unit: reason}`` for those that failed. A unit whose read fails with a
    :data:`TRANSIENT` error is tried again, up to :data:`TRIES` times, in a
    fresh fork; any other error fails it at once. The caller merges the
    forks into ``session`` and commits once: many sessions committing to one
    branch would each rebase onto the others, which is slow and fails under
    contention (docs/ingest.md).

    Threads, not processes: a slide's time is spent waiting on range reads
    and in code that releases the GIL (numpy, ElementTree, ITK), and a fork
    handed to a thread needs no pickling. A line is printed every
    ``progress_every`` seconds whether or not a slide finished, so a hung
    read shows as a count that stops moving.
    """
    started = time.monotonic()
    done: dict = {}
    failed: dict[str, str] = {}

    def attempt(unit: str):
        for tries in range(1, TRIES + 1):
            fork = session.fork()
            began = time.monotonic()
            try:
                result = job(fork, unit)
                say(f"{unit}: written in {time.monotonic() - began:.1f} s")
                return fork, result
            except TRANSIENT as error:
                if tries == TRIES:
                    raise
                say(f"{unit}: {type(error).__name__}: {error}; try {tries + 1} of {TRIES} in {RETRY_AFTER:.0f} s")
                time.sleep(RETRY_AFTER)

    with ThreadPoolExecutor(threads) as pool:
        futures = {pool.submit(attempt, unit): unit for unit in units}
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=progress_every, return_when=FIRST_COMPLETED)
            for future in finished:
                unit = futures[future]
                try:
                    done[unit] = future.result()
                except Exception as error:  # noqa: BLE001 -- reported with the others, nothing committed
                    failed[unit] = f"{type(error).__name__}: {error}"
            count = len(done) + len(failed)
            elapsed = time.monotonic() - started
            rate = count / elapsed
            left = f", about {(len(units) - count) / rate:.0f} s left" if rate else ""
            say(f"{count} of {len(units)} slides parsed ({len(failed)} failed) in {elapsed:.0f} s{left}")
    return done, failed
