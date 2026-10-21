"""Shared remote read-time benchmark: what a user actually waits for.

## The metric: read TIME under remote conditions (never local)

Report the end-to-end wall-clock **read time a user experiences**, measured under
realistic network latency (via ``snailmail``'s injected lognormal latency + optional
shared-pipe bandwidth, or a real remote store). **Never headline a local time** --
locally it is memory-bandwidth-bound, so virtualization only adds indirection and
reads *slower*, which is irrelevant to the question. And **do not report GET counts
or round-trips** -- that is internal mechanism, not what the user feels. Report
seconds.

## What we compare: download-then-read vs. virtual lazy read, over a COLLECTION

The realistic alternative to a virtual store is to **fully download the source
file(s)** and open each the normal way (its own library: mrcfile, h5py, czifile).
Under latency the virtual path wins because it fetches only the byte ranges a query
touches and never re-downloads whole files or re-walks their scattered internal
metadata -- and a **collection amplifies it**: N files to otherwise pull in full.
:func:`compare_remote` measures both, in seconds, across a latency sweep, on a
collection.

## Cold cache, honestly

Icechunk (and the HTTP store) cache fetched bytes process-globally, so a warm process
would read zero bytes on the second pass and report a fictional zero. Every timed read
therefore runs in a **fresh spawned subprocess**; the ``snailmail`` server injects the
same latency per request regardless. Because the work runs in a subprocess, a
:class:`RemoteCollectionTarget` must be picklable (a small dataclass of paths/params)
and importable from its module.

snailmail is the bought latency server -- never hand-roll one.
"""
from __future__ import annotations

import hashlib
import multiprocessing as mp
import os
import queue as _queue
import shutil
import tempfile
import time
from functools import partial
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class RemoteCollectionTarget(Protocol):
    """What a subproject supplies so the shared runner can benchmark its format.

    Must be **picklable** (it is sent to a spawned subprocess) -- a small dataclass
    holding source paths and read parameters, no open handles. The reads must
    return the *same* bytes for the same file, so a virtual read and a
    download-then-read of the collection are directly comparable (the runner asserts
    their digests match).

    A target may also define ``read_remote(urls) -> list[np.ndarray]``: the same
    region read by streaming the source in place over range requests with its own
    library (h5py + remfile, say). It is optional -- not every format's library can
    read a remote file at all -- and stays off this Protocol's required surface so
    an ``isinstance`` check still passes without it. :func:`compare_remote` times it
    as a third method when it is there.
    """

    #: The collection's local source files (they must share one directory, which the
    #: benchmark server serves; each file is addressed by its basename).
    source_files: list[str]

    def build_repo(self, urls: list[str], repo_path: str) -> None:
        """Virtualize the collection into one Icechunk repo at ``repo_path``.

        ``urls`` are the ``http(s)`` URLs of the source files on the benchmark
        server, in ``source_files`` order; the built repo must *reference* those
        URLs (needs HTTP virtual-chunk containers -- see
        :mod:`virtual_core.icechunk_io`), so its reads go over the network.
        """
        ...

    def read_virtual(self, repo_path: str) -> list[np.ndarray]:
        """Read the representative region of every file **through the repo** (lazy;
        fetches only the byte ranges it needs)."""
        ...

    def read_native(self, local_paths: list[str]) -> list[np.ndarray]:
        """Read the same representative region of every file the STANDARD way (the
        format's own library) from already-downloaded local copies, in the order of
        ``local_paths`` (which matches ``source_files``)."""
        ...


def _digest(arrays: list[np.ndarray]):
    """A cheap order-preserving fingerprint of a read's result, for the round-trip
    assertion across two independent IO paths (and two processes)."""
    return tuple(
        (tuple(np.asarray(a).shape), str(np.asarray(a).dtype),
         hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest())
        for a in arrays
    )


def _virtual_job(target, repo_path, q):  # runs in a fresh (cold-cache) subprocess
    t = time.perf_counter()
    arrays = target.read_virtual(repo_path)
    elapsed = time.perf_counter() - t
    q.put((elapsed, _digest(arrays)))


def _download_job(target, urls, dest_dir, q):  # fresh subprocess: pull, then read
    from virtual_core.fetch import download_then_index

    t = time.perf_counter()
    local_paths = [download_then_index(u, dest_dir) for u in urls]  # full files, over the link
    arrays = target.read_native(local_paths)
    elapsed = time.perf_counter() - t
    q.put((elapsed, _digest(arrays)))


def _remote_job(target, urls, q):  # fresh subprocess: stream the source in place, no download
    t = time.perf_counter()
    arrays = target.read_remote(urls)
    elapsed = time.perf_counter() - t
    q.put((elapsed, _digest(arrays)))


def _run_cold(server, fn, *args):
    """Run ``fn(*args, q)`` in a fresh (cold-cache) spawned process while counting the
    bytes it pulls from ``server``. Returns ``(elapsed_seconds, bytes_transferred,
    digest)`` -- ``elapsed`` is timed inside the child (excludes spawn overhead) and
    ``bytes`` is what actually crossed the wire (the "total download amount")."""
    server.reset_counts()
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=fn, args=(*args, q))
    p.start()
    # Poll rather than a bare q.get(): if the child dies before putting a result
    # (read raises, OOM, C-level crash) a bare get() would block forever. A slow but
    # live read just loops and keeps waiting.
    while True:
        try:
            elapsed, digest = q.get(timeout=2.0)
            break
        except _queue.Empty:
            if not p.is_alive():
                p.join()
                raise RuntimeError(
                    f"benchmark subprocess died (exit code {p.exitcode}) before "
                    "returning a result"
                )
    p.join()
    if p.exitcode:
        raise RuntimeError(f"benchmark subprocess exited with code {p.exitcode}")
    return elapsed, int(server.stats().get("total_bytes", 0)), digest


def compare_remote(
    target: RemoteCollectionTarget,
    *,
    latencies_ms: tuple[float, ...] = (10.0, 50.0, 100.0),
    trials: int = 3,
    bandwidth_mbs: float | None = None,
    workdir: str | None = None,
    seed: int = 0,
) -> list[dict]:
    """Time **download-then-read vs. virtual lazy read** over a collection.

    The query spans the whole collection -- the representative region of *every*
    file -- which is where virtualization wins most: N small (coalesced) range reads
    vs. downloading N whole files. For each latency in ``latencies_ms`` the collection
    is read both ways, each in a cold subprocess, ``trials`` times. Returns a flat
    list of records::

        {"latency_ms": float, "method": "download"|"virtual"|"h5py_remote",
         "trial": int, "seconds": float, "bytes": int}

    A target that defines ``read_remote`` (stream the source in place via its own
    library, e.g. h5py over range requests) gets a third method, ``h5py_remote``,
    timed alongside the other two.

    Two honest metrics, no round-trip counts: **seconds** a user waits (under injected
    latency) and **bytes actually moved** (the total download amount -- data volume,
    hence transfer cost, independent of bandwidth). The two paths are asserted
    byte-identical before timing. ``bandwidth_mbs`` optionally caps the shared pipe
    (leave ``None`` for latency-only, so the byte-volume gap speaks for itself);
    ``workdir`` holds the repo and per-trial download scratch (a temp dir by default).
    """
    from snailmail import Fixed, HTTPRangeServer, LogNormal

    files = [os.path.abspath(f) for f in target.source_files]
    if len(files) < 1:
        raise ValueError("compare_remote needs at least one source file")
    root = os.path.dirname(files[0])
    if any(os.path.dirname(f) != root for f in files):
        raise ValueError("all source files must share one directory (the served root)")
    keys = [os.path.basename(f) for f in files]

    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="compare_remote_")
    repo_path = os.path.join(workdir, "repo")
    shutil.rmtree(repo_path, ignore_errors=True)

    server = HTTPRangeServer(root, bandwidth_mbs=bandwidth_mbs).start()
    records: list[dict] = []
    try:
        urls = [server.url(k) for k in keys]
        target.build_repo(urls, repo_path)  # built at zero latency; references the URLs

        def latency(ms):
            return LogNormal(mode_ms=ms, seed=seed) if ms else Fixed(0.0)

        has_remote = hasattr(target, "read_remote")

        # Round-trip check once (cheap latency): every path must return the same bytes.
        server.set_latency(latency(0))
        _, _, dig_v = _run_cold(server, _virtual_job, target, repo_path)
        _, _, dig_d = _run_cold(server, _download_job, target, urls, os.path.join(workdir, "dl_check"))
        if dig_v != dig_d:
            raise AssertionError("virtual and downloaded reads disagree (not byte-identical)")
        if has_remote:
            _, _, dig_r = _run_cold(server, _remote_job, target, urls)
            if dig_r != dig_v:
                raise AssertionError("remote (streamed) read disagrees (not byte-identical)")

        for ms in latencies_ms:
            server.set_latency(latency(ms))
            for trial in range(trials):
                d_dir = os.path.join(workdir, f"dl_{int(ms)}_{trial}")
                jobs = [
                    ("virtual", partial(_virtual_job, target, repo_path)),
                    ("download", partial(_download_job, target, urls, d_dir)),
                ]
                if has_remote:
                    jobs.append(("h5py_remote", partial(_remote_job, target, urls)))
                for method, job in jobs:
                    seconds, nbytes, _ = _run_cold(server, job)
                    records.append({"latency_ms": ms, "method": method, "trial": trial,
                                    "seconds": seconds, "bytes": nbytes})
    finally:
        server.stop()
        if own_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return records


def summarize(records: list[dict]) -> dict:
    """Plot-ready reduction of :func:`compare_remote`'s records (median over trials).

    Returns ``{method: {"seconds": {latency_ms: median_s}, "bytes": median_bytes}}``.
    ``bytes`` is a single number per method (the read moves the same data at every
    latency), so it summarizes the total download amount directly.
    """
    secs: dict[tuple[str, float], list[float]] = {}
    byts: dict[str, list[int]] = {}
    for r in records:
        secs.setdefault((r["method"], r["latency_ms"]), []).append(r["seconds"])
        byts.setdefault(r["method"], []).append(r.get("bytes", 0))
    out: dict[str, dict] = {}
    for (method, ms), vals in secs.items():
        out.setdefault(method, {"seconds": {}})["seconds"][ms] = float(np.median(vals))
    for method, bvals in byts.items():
        out[method]["seconds"] = dict(sorted(out[method]["seconds"].items()))
        out[method]["bytes"] = float(np.median(bvals))
    return out
