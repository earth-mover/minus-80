"""Local copies of remote files, for a parser to index.

A parser that walks metadata scattered through a file (an HDF5 chunk B-tree, a
TIFF directory chain, an MRC extended header) makes one latency-bound round trip
per structure when it reads over HTTP, and a large file has thousands of them.
These functions fetch the bytes the parser needs in concurrent block-sized range
GETs and write them to a local file, so the walk runs at disk speed. The manifest
still references the remote URL; the local copy is only read during indexing.

Every range read in minus80 goes through :func:`read_range` or
:func:`iter_ranges`, so each checks that the server sent every byte asked for:
obstore returns a short body without complaint, and a short write into a
pre-sized file leaves a run of zeros that reads as data.

Each function takes an optional ``registry``. A parser handed one by its
caller passes it on, so every read goes through the stores the caller
configured; without one, the store comes from
:func:`~minus80.core.registry.store_and_key`.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path
from typing import BinaryIO

from obspec_utils.registry import ObjectStoreRegistry

from .aio import own_loop, submit
from .progress import Progress
from .registry import as_url, file_path, resolve, store_and_key

BLOCK = 32 << 20
CONCURRENCY = 16

#: Seconds a piece may be in flight before :func:`iter_ranges` asks for it a
#: second time and takes whichever answer comes first. A server can leave one
#: request waiting for minutes while the rest finish in seconds; EBI did so for
#: 60 to 90 s at a time while serving a 64 MiB piece usually took under 10 s.
#: A piece that takes longer than this is also reported.
HEDGE_AFTER = 20.0


def cache_dir() -> str:
    """``$MINUS80_CACHE_DIR``, else ``$XDG_CACHE_HOME/minus80``, else ``~/.cache/minus80``."""
    if path := os.environ.get("MINUS80_CACHE_DIR"):
        return path
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "minus80")


def object_size(url: str, *, patient: bool = False,
                registry: ObjectStoreRegistry | None = None) -> int:
    """The length in bytes of the object at ``url``, from one HEAD."""
    import obstore

    store, key = _store_and_key(url, patient, registry)
    return int(obstore.head(store, key)["size"])


def read_range(url: str, start: int, end: int, *, patient: bool = False,
               registry: ObjectStoreRegistry | None = None) -> bytes:
    """Bytes ``[start, end)`` of ``url`` in one GET.

    Raises ``OSError`` unless exactly ``end - start`` bytes come back.
    """
    store, key = _store_and_key(url, patient, registry)
    return _get(store, key, url, start, end)


def iter_ranges(
    url: str,
    ranges: Iterable[tuple[int, int]],
    *,
    block: int = BLOCK,
    concurrency: int = CONCURRENCY,
    progress: bool = True,
    patient: bool = False,
    registry: ObjectStoreRegistry | None = None,
    window: int | None = None,
    hedge_after: float | None = HEDGE_AFTER,
) -> Iterator[tuple[int, bytes]]:
    """Each ``[start, end)`` range of ``url`` as ``(start, data)``, in the order given.

    A range longer than ``block`` is split into pieces of at most ``block``
    bytes, yielded in order. Every piece is its own GET; at most
    ``concurrency`` are in flight, and pieces are yielded as soon as they and
    every piece before them have arrived. Requests go on starting while an
    earlier piece is still out, up to ``window`` pieces (default
    ``2 * concurrency``) asked for and not yet yielded, so one slow piece does
    not leave the other connections idle; memory stays near ``window * block``.
    A piece still out after ``hedge_after`` seconds is asked for again, and a
    line reports each piece that took that long. A short piece raises
    ``OSError``.

    Pieces are fetched one ``get_range`` each because ``obstore.get_ranges``
    merges adjacent ranges into one request even with ``coalesce=0``, which
    turns a batch of blocks into one GET of all of them (docs/upstream.md).

    With ``progress``, prints a :class:`~minus80.core.progress.Progress` line
    labelled with the file name, counting bytes against the total asked for.
    """
    pieces = [(s, min(s + block, end)) for start, end in ranges for s in range(start, end, block)]
    fetched = _fetch_pieces(url, pieces, concurrency, window or 2 * concurrency, hedge_after, patient, registry)
    if not progress:
        yield from fetched
        return
    name = url.rsplit("/", 1)[-1]
    total = sum(end - start for start, end in pieces)
    with Progress(f"fetching {name}", total, unit="MiB", scale=1 << 20) as note:
        for start, data in fetched:
            note.advance(len(data))
            yield start, data


def write_ranges(url: str, ranges: Iterable[tuple[int, int]], f: BinaryIO, **kw) -> int:
    """Write each range of ``url`` into ``f`` at its own offset; return bytes written.

    ``kw`` is passed to :func:`iter_ranges`.
    """
    written = 0
    for start, data in iter_ranges(url, ranges, **kw):
        f.seek(start)
        f.write(data)
        written += len(data)
    return written


def download_then_index(
    url: str,
    dest_dir: str | None = None,
    *,
    block: int = BLOCK,
    concurrency: int = CONCURRENCY,
    registry: ObjectStoreRegistry | None = None,
) -> str:
    """Fetch ``url`` into ``dest_dir`` with parallel range GETs and return the path.

    ``block`` is the size of each range GET and ``concurrency`` how many are in
    flight at once. ``dest_dir`` defaults to :func:`cache_dir`. A copy already
    there is reused. Hand the returned path to a parser and keep ``url`` in the
    manifest.
    """
    # Two sources can share a filename and hold different bytes: idr0095 names
    # 412 files with 19 distinct basenames, and `1X.nd2` alone names 54 of them.
    # A cache keyed on the basename would silently hand the first file's bytes
    # to all 54. The containing directory picks the cache slot, and the basename
    # is kept inside it because parsers sniff the extension.
    name = url.rsplit("/", 1)[-1]
    slot = hashlib.sha256(url[: len(url) - len(name)].encode()).hexdigest()[:16]
    dest = _cache_path(dest_dir, slot, name)
    if os.path.exists(dest):
        return dest
    size = object_size(url, registry=registry)
    _write_part(dest, size, lambda f: write_ranges(
        url, [(0, size)], f, block=block, concurrency=concurrency, registry=registry))
    return dest


def sparse_local(
    url: str,
    regions: Sequence[tuple[int, int]],
    dest_dir: str | None = None,
    *,
    block: int = BLOCK,
    concurrency: int = CONCURRENCY,
    registry: ObjectStoreRegistry | None = None,
) -> str:
    """Fetch only ``regions`` of ``url`` into a local file that is otherwise a hole.

    The file is created at the source's full length and the named byte ranges
    are written at their true offsets; everything else is a sparse hole that
    takes no disk space. A parser handed the result sees a file of the right
    size with the parts it needs at the offsets it expects. Writing 8 MiB of a
    156 GiB file this way uses 8 MB of disk.

    Use it for a parser such as ``tifffile`` or ``h5py`` that drives a file
    object with seeks and cannot be given a list of regions to fetch. The caller
    decides which ranges matter, for instance one contiguous metadata region,
    and fetches them in bulk; the parser then seeks freely against a local
    file. A parser that reads bytes directly, as ``async-tiff`` does, can walk
    the fetched region in memory instead (docs/upstream.md).

    ``dest_dir`` defaults to :func:`cache_dir`. The cache is keyed on the
    source and the regions, so a later call asking for different ranges does
    not get the earlier file.
    """
    name = url.rsplit("/", 1)[-1]
    wanted = sorted((int(a), int(b)) for a, b in regions)
    if not wanted:
        raise ValueError(f"no regions asked for from {url}")
    signature = repr((url[: len(url) - len(name)], wanted)).encode()
    slot = hashlib.sha256(signature).hexdigest()[:16]
    dest = _cache_path(dest_dir, slot, name)
    if os.path.exists(dest):
        return dest
    size = object_size(url, registry=registry)
    if wanted[-1][1] > size:
        raise ValueError(f"{url}: asked for bytes past the end of a {size}-byte file")
    _write_part(dest, size, lambda f: write_ranges(
        url, wanted, f, block=block, concurrency=concurrency, registry=registry))
    return dest


def local_copy(source: str | Path, dest_dir: str | None = None) -> str:
    """A local path holding ``source``'s bytes: the file itself when local, else a cached download."""
    path = file_path(source)
    if path is not None:
        return str(path)
    return download_then_index(as_url(source), dest_dir)


def _cache_path(dest_dir: str | None, slot: str, name: str) -> str:
    # Absolute: a relative dest makes file://<rel> parse its first segment as a
    # host, which breaks registry prefix-stripping downstream.
    return os.path.abspath(os.path.join(dest_dir or cache_dir(), slot, name))


def _write_part(dest: str, size: int, fill) -> None:
    """Create ``dest`` at ``size`` bytes, filled by ``fill(f)``.

    ``f.truncate`` pre-sizes the file, so a partial download would have the
    right size and pass for a cache hit. Writing to ``.part`` and renaming at
    the end keeps an interrupted download out of the cache.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    with open(part, "wb") as f:
        f.truncate(size)
        fill(f)
    os.replace(part, dest)


def _store_and_key(url: str, patient: bool, registry: ObjectStoreRegistry | None) -> tuple:
    if registry is not None:
        return resolve(registry, url)
    return store_and_key(url, patient=patient)


def _get(store, key: str, url: str, start: int, end: int) -> bytes:
    import obstore

    if end <= start:
        return b""
    return _checked(obstore.get_range(store, key, start=start, end=end), url, start, end)


async def _get_async(store, key: str, url: str, start: int, end: int) -> bytes:
    import obstore

    if end <= start:
        return b""
    return _checked(await obstore.get_range_async(store, key, start=start, end=end), url, start, end)


def _checked(buf, url: str, start: int, end: int) -> bytes:
    data = bytes(buf)
    if len(data) != end - start:
        raise OSError(f"short range read for {url} bytes [{start}, {end}): "
                      f"got {len(data)} of {end - start}")
    return data


class _Piece:
    """One piece of :func:`iter_ranges`, with every request made for it."""

    def __init__(self, start: int, end: int, request: Callable[[], Future[bytes]]) -> None:
        self.start, self.end, self._request = start, end, request
        self.began = time.monotonic()
        #: When the first request for the piece finished, for the slow-piece report.
        self.finished: float | None = None
        self.requests: list[Future[bytes]] = []
        self.ask()

    def ask(self) -> None:
        future = self._request()
        future.add_done_callback(self._done)
        self.requests.append(future)

    def _done(self, _future: Future) -> None:
        if self.finished is None:
            self.finished = time.monotonic()

    def in_flight(self) -> int:
        return sum(not future.done() for future in self.requests)

    def _answer(self) -> Future[bytes] | None:
        return next((f for f in self.requests if f.done() and not f.cancelled() and f.exception() is None), None)

    def ready(self) -> bool:
        """True once one request has answered, or every request has failed."""
        return self._answer() is not None or all(future.done() for future in self.requests)

    def result(self) -> bytes:
        answer = self._answer()
        self.cancel()
        return (answer or self.requests[0]).result()

    def cancel(self) -> None:
        for future in self.requests:
            future.cancel()


def _fetch_pieces(url: str, pieces: list[tuple[int, int]], concurrency: int, window: int,
                  hedge_after: float | None, patient: bool,
                  registry: ObjectStoreRegistry | None) -> Iterator[tuple[int, bytes]]:
    # Each GET is a coroutine rather than a thread-pool task because a running
    # thread cannot be stopped: after an error or an early stop by the caller,
    # a patient store's GETs would keep retrying for up to twenty minutes and
    # hold the interpreter open at exit. Cancelling the coroutine drops the
    # request. The GETs run on a loop of their own, so a stall on zarr's loop,
    # which carries every write, does not stop them.
    store, key = _store_and_key(url, patient, registry)
    loop = own_loop("fetch")
    name = url.rsplit("/", 1)[-1]
    todo = deque(pieces)
    started: deque[_Piece] = deque()
    window = max(window, concurrency)
    try:
        while todo or started:
            while todo and len(started) < window and sum(p.in_flight() for p in started) < concurrency:
                start, end = todo.popleft()
                started.append(_Piece(start, end, lambda s=start, e=end: submit(_get_async(store, key, url, s, e), loop)))
            head = started[0]
            if head.ready():
                started.popleft()
                data = head.result()
                took = (head.finished or time.monotonic()) - head.began
                if hedge_after is not None and took > hedge_after:
                    print(f"      slow range [{head.start:,}, {head.end:,}) of {name}: {took:.0f} s"
                          f"{', asked for twice' if len(head.requests) > 1 else ''}", flush=True)
                yield head.start, data
                continue
            timeout = None
            if hedge_after is not None and len(head.requests) == 1:
                timeout = head.began + hedge_after - time.monotonic()
                if timeout <= 0:
                    head.ask()
                    continue
            waiting = [f for piece in started for f in piece.requests if not f.done()]
            wait(waiting, timeout=timeout, return_when=FIRST_COMPLETED)
    finally:
        for piece in started:
            piece.cancel()
