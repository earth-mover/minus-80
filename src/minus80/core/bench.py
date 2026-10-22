"""Time reading a collection through a virtual repo against downloading it, under network latency.

:func:`compare_remote` serves a target's source files from a local HTTP range
server that injects lognormal latency per request, with an optional bandwidth
cap. The target builds a virtual repo referencing those URLs. Then, at each
latency, it times these reads of the same region of every file:

- ``virtual``: through the repo, which fetches just the byte ranges the read needs;
- ``download``: fetching each whole file with
  :func:`~minus80.core.fetch.download_then_index`, then reading it with the
  format's own library (mrcfile, h5py, czifile);
- ``h5py_remote``, when the target defines ``read_remote``: streaming each file
  in place with its own library over range requests.

The results are wall-clock seconds and bytes transferred. A local read time is
not a useful comparison: on local disk the virtual path only adds indirection.

Icechunk and the HTTP store cache fetched bytes for the life of a process, so a
second read in the same process would move nothing. Every timed read therefore
runs in a freshly spawned subprocess, which means a
:class:`RemoteCollectionTarget` must be picklable (a small dataclass of paths
and parameters) and importable from its module.

The latency server is imported inside :func:`compare_remote` and is not a
dependency of minus-80.
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
    """What a format supplies so :func:`compare_remote` can benchmark it.

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
        URLs, through HTTP virtual chunk containers (see
        :mod:`minus80.core.icechunk_io`), so its reads go over the network.
        """
        ...

    def read_virtual(self, repo_path: str) -> list[np.ndarray]:
        """Read the representative region of every file through the repo."""
        ...

    def read_native(self, local_paths: list[str]) -> list[np.ndarray]:
        """Read the same region of every file with the format's own library.

        ``local_paths`` are downloaded copies, in ``source_files`` order.
        """
        ...


def _digest(arrays: list[np.ndarray]):
    """Shape, dtype and SHA-1 of each array, in order, to compare reads across processes."""
    return tuple(
        (tuple(np.asarray(a).shape), str(np.asarray(a).dtype),
         hashlib.sha1(np.ascontiguousarray(a).tobytes()).hexdigest())
        for a in arrays
    )


def _virtual_job(target, repo_path, q):
    t = time.perf_counter()
    arrays = target.read_virtual(repo_path)
    elapsed = time.perf_counter() - t
    q.put((elapsed, _digest(arrays)))


def _download_job(target, urls, dest_dir, q):
    from minus80.core.fetch import download_then_index

    t = time.perf_counter()
    local_paths = [download_then_index(u, dest_dir) for u in urls]
    arrays = target.read_native(local_paths)
    elapsed = time.perf_counter() - t
    q.put((elapsed, _digest(arrays)))


def _remote_job(target, urls, q):
    t = time.perf_counter()
    arrays = target.read_remote(urls)
    elapsed = time.perf_counter() - t
    q.put((elapsed, _digest(arrays)))


def _run_cold(server, fn, *args):
    """Run ``fn(*args, q)`` in a freshly spawned process, counting the bytes it pulls from ``server``.

    Returns ``(elapsed_seconds, bytes_transferred, digest)``. ``elapsed`` is
    timed inside the child, so it excludes spawn overhead.
    """
    server.reset_counts()
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=fn, args=(*args, q))
    p.start()
    # A bare q.get() blocks forever if the child dies before putting a result
    # (an exception, OOM, a crash in C). Polling notices the death; a slow but
    # live read keeps looping.
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
    """Time a virtual read of a collection against downloading it and reading locally.

    Each read covers the representative region of every file in the
    collection. For each latency in ``latencies_ms`` the collection is read
    each way ``trials`` times, each read in a fresh subprocess. Returns a flat
    list of records::

        {"latency_ms": float, "method": "download"|"virtual"|"h5py_remote",
         "trial": int, "seconds": float, "bytes": int}

    A target that defines ``read_remote`` gets a third method, ``h5py_remote``.

    Before timing, every method's result is read once at zero latency and must
    be byte-identical, or an ``AssertionError`` is raised. ``bandwidth_mbs``
    caps the server's bandwidth; ``None`` leaves only latency. ``workdir``
    holds the repo and the per-trial downloads, and defaults to a temporary
    directory that is removed afterwards.
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
        target.build_repo(urls, repo_path)

        def latency(ms):
            return LogNormal(mode_ms=ms, seed=seed) if ms else Fixed(0.0)

        has_remote = hasattr(target, "read_remote")

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
    """Median over trials of :func:`compare_remote`'s records.

    Returns ``{method: {"seconds": {latency_ms: median_s}, "bytes": median_bytes}}``.
    ``bytes`` is one number per method, the median over every latency, because a
    read moves the same data at every latency.
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
