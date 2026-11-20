"""The parts of the MICrONS ingest that are not about the data: listings, repos and workers.

``ingest.py`` holds what the archive publishes and how the store holds it.
This module holds what any ingest of this shape needs around that: listing a
bucket prefix, asking the DANDI API where a dandiset's files are, opening the
repo, and running one worker process per scan, each writing into its own fork
of one session.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from urllib.parse import urlparse

from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, create_repo, storage_for
from minus80.core.progress import Progress

DANDI_API = "https://api.dandiarchive.org/api"

#: Seconds between the driver's progress lines while workers run.
PROGRESS_EVERY = 30.0


def list_prefix(prefix: str) -> dict[str, int]:
    """Every object under an ``s3://`` prefix, by name relative to it, with its size.

    One LIST request per thousand objects, read anonymously: the BossDB bucket
    is public.
    """
    import obstore
    from obstore.store import S3Store

    from minus80.core.registry import s3_region

    parsed = urlparse(prefix)
    store = S3Store(parsed.netloc, region=s3_region(parsed.netloc), skip_signature=True)
    key = parsed.path.lstrip("/")
    return {o["path"][len(key):]: int(o["size"])
            for batch in obstore.list(store, prefix=key) for o in batch}


def dandi_assets(dandiset: str, version: str) -> dict[str, dict]:
    """The assets of one published version of a dandiset, by path, each with its ``s3://`` URL.

    The listing gives each asset's path, size and id. Where its bytes sit is
    in the asset's own metadata, as ``contentUrl``; the S3 entry there is the
    one a range reader can use.
    """
    found: dict[str, dict] = {}
    url = f"{DANDI_API}/dandisets/{dandiset}/versions/{version}/assets/?page_size=200"
    while url:
        page = _get_json(url)
        for asset in page["results"]:
            found[asset["path"]] = {"asset_id": asset["asset_id"], "size": asset["size"]}
        url = page.get("next")
    for path, asset in found.items():
        metadata = _get_json(f"{DANDI_API}/assets/{asset['asset_id']}/")
        s3 = [u for u in metadata["contentUrl"] if ".s3.amazonaws.com/" in u]
        if len(s3) != 1:
            raise ValueError(f"{path}: expected one S3 content URL, found {metadata['contentUrl']}")
        bucket, _, key = s3[0].removeprefix("https://").partition(".s3.amazonaws.com/")
        asset["url"] = f"s3://{bucket}/{key}"
    return found


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)


def open_for_writing(location: str, source_prefix: str):
    """The repo at ``location``, created if it is a new local or ``s3://`` location.

    An ``arraylake://org/repo`` location names a repo that already exists and
    whose virtual chunk container covers ``source_prefix``; otherwise a repo
    is created with that prefix as its one container, or reopened.
    """
    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location, source_prefixes=[source_prefix])
    import icechunk

    if icechunk.Repository.exists(storage_for(location)):
        return create_repo(location, [], prefixes=[source_prefix], open_existing=True)
    return create_repo(location, [], prefixes=[source_prefix])


def run_forks(session, jobs: Mapping[str, tuple[Callable, tuple]], *, parallel: int,
              what: str) -> tuple[dict, dict[str, str]]:
    """Run each job in a worker process with a fork of ``session``, then merge the forks.

    ``jobs`` maps a name to a function and its arguments; the function is
    called as ``function(fork, *arguments)`` and returns ``(fork, summary)``.
    Returns the summary of every job that finished and the error of each that
    failed. Only finished jobs' forks are merged into ``session``; the caller
    commits.
    """
    forks = {name: session.fork() for name in jobs}
    done, failed = {}, {}
    # spawn, not fork: a forked child would inherit the parent's Icechunk and
    # obstore runtime threads mid-state, which can deadlock.
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(parallel, mp_context=context) as pool, \
            Progress(what, len(jobs), unit="scans", every=PROGRESS_EVERY) as progress:
        futures = {pool.submit(function, forks[name], *arguments): name
                   for name, (function, arguments) in jobs.items()}
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=PROGRESS_EVERY, return_when=FIRST_COMPLETED)
            for future in finished:
                name = futures[future]
                try:
                    done[name] = future.result()
                except Exception as exc:  # noqa: BLE001 -- reported, and the other scans go on
                    print(f"{name}: FAILED, {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    failed[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            progress.advance(len(finished))
    if done:
        started = time.monotonic()
        session.merge(*(fork for fork, _ in done.values()))
        print(f"merged {len(done)} forks in {time.monotonic() - started:.1f} s", flush=True)
    return {name: summary for name, (_, summary) in done.items()}, failed
