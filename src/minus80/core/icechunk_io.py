"""Persist a ManifestGroup to Icechunk, and reopen it -- shared by all subprojects.

The subtle, reusable parts live here:

* one :class:`icechunk.VirtualChunkContainer` per source prefix, so reads resolve
  references back into the original files;
* ``authorize_virtual_chunk_access`` gating (Icechunk refuses virtual reads unless
  the source prefix is authorized).

Local-filesystem, ``s3://`` and ``http(s)`` sources are handled directly (the last
so a repo can reference bytes served over the network -- e.g. a ``snailmail``
benchmark server or a real object store fronted by HTTP); for other cloud schemes
pass your own already-configured registry/store.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

import zarr
from virtualizarr.manifests import ManifestGroup

from .registry import s3_anonymous, s3_region, url_dir


def _url_prefix(url: str) -> str:
    """The virtual-chunk-container prefix (the source's containing "directory").

    Local paths resolve to a ``file://`` directory URI; ``http(s)`` URLs keep their
    scheme/host and drop the final path segment. One prefix groups every source that
    shares it into a single container.
    """
    if urlparse(url).scheme in ("", "file"):
        # unquote so the prefix matches registry_for's (which unquotes too); without
        # it a source dir with a space etc. re-encodes to %2520 and never matches.
        return Path(unquote(urlparse(url).path)).resolve().parent.as_uri() + "/"
    return url_dir(url)


def _access_for(prefix: str):
    """Icechunk virtual-chunk-access credential for a source prefix (replaces the
    deprecated ``None`` sentinel). Local sources use ``LocalFileSystemAccess``;
    ``s3://`` uses anonymous or env credentials (see
    :func:`minus80.core.registry.s3_anonymous`); ``http(s)`` sources use the
    ``HttpAccess`` sentinel (anonymous -- the store config carries the connection
    details)."""
    import icechunk

    scheme = urlparse(prefix).scheme
    if scheme in ("", "file"):
        return icechunk.credentials.LocalFileSystemAccess
    if scheme == "s3":
        cred = (
            icechunk.credentials.s3_anonymous_credentials()
            if s3_anonymous()
            else icechunk.credentials.s3_from_env_credentials()
        )
        return icechunk.credentials.Credentials.S3(cred)  # authorize wants the wrapper
    return icechunk.credentials.HttpAccess


def _store_for(prefix: str):
    """The Icechunk object store backing a container prefix.

    ``s3://`` uses ``s3_store`` (region and anonymity from the environment, see
    :mod:`minus80.core.registry`) so a repo of references into a public S3 bucket
    resolves without local credentials -- and so it is Arraylake-ingestable, which
    blocks HTTP-backed containers but accepts S3-backed ones. ``http(s)`` uses
    ``http_store`` with ``allow_http`` so plain-http origins work (a localhost
    ``snailmail`` benchmark server, an internal cache); TLS origins are equally served.
    """
    import icechunk

    scheme = urlparse(prefix).scheme
    if scheme in ("", "file"):
        return icechunk.local_filesystem_store(urlparse(prefix).path)
    if scheme == "s3":
        return icechunk.s3_store(region=s3_region(), anonymous=s3_anonymous())
    if scheme in ("http", "https"):
        return icechunk.http_store({"allow_http": "true"})
    raise ValueError(
        f"no built-in virtual-chunk store for scheme {scheme!r} ({prefix}); pass a "
        "pre-configured registry/store for other cloud backends."
    )


# Object storage wants a large "ideal request size" so a multi-chunk read merges into
# one big range GET; a locally-stored repo defaults this to a tiny (4 KiB) local-disk
# value that a remote virtual read would inherit, shattering a read into dozens of
# latency-bound GETs (icechunk#2256). We set the remote-appropriate size explicitly
# whenever any source is remote, so reads coalesce to a handful of ~12 MiB requests.
REMOTE_IDEAL_REQUEST_SIZE = 12 * 1024 * 1024


#: Icechunk fetches manifests one at a time while committing unless told otherwise,
#: which makes commit cost scale with the number of arrays times the round trip --
#: 11.45s versus 0.83s for 200 arrays at 50 ms. Nothing wants the default of 1, so
#: it is raised here rather than offered as a knob every caller has to remember.
MANIFEST_FETCH_CONCURRENCY = 32


def manifest_config():
    """Icechunk's manifest settings, or None where this version has no such knob.

    The subprojects sharing this module pin different icechunk versions, and the
    concurrency argument does not exist in all of them. Committing serially is
    slower, not wrong, so a version without the argument keeps its default
    rather than failing to build a repo at all.
    """
    import icechunk

    try:
        return icechunk.ManifestConfig(
            max_concurrent_manifest_fetches_during_commit=MANIFEST_FETCH_CONCURRENCY
        )
    except TypeError:
        return None


#: How many times a commit may rebase onto whatever landed on the branch first.
#: A long ingest commits once per source file and holds its session across a
#: whole file's indexing, so another writer -- a second ingest, a metadata
#: restatement -- has a wide window to commit first.
COMMIT_REBASE_TRIES = 100


def commit(session, message: str, *, tries: int = COMMIT_REBASE_TRIES) -> str:
    """Commit a writable session, rebasing onto concurrent commits on its branch.

    A commit from a session that no longer sits at the branch tip fails outright.
    Icechunk's own conflict solver replays this session's changes on top of the
    newer ancestry, which resolves the case every ingest hits: two writers
    touching different nodes of the same store. A conflict the solver cannot
    resolve -- the same array written twice, differently -- still raises
    ``RebaseFailedError``, because picking a winner there is the caller's call.
    """
    import inspect

    import icechunk

    solver = icechunk.BasicConflictSolver()
    try:
        takes_solver = "rebase_with" in inspect.signature(session.commit).parameters
    except (TypeError, ValueError):
        takes_solver = False
    if takes_solver:
        return session.commit(message, rebase_with=solver, rebase_tries=tries)
    # Older icechunk commits without rebasing; drive its own rebase by hand.
    for _ in range(tries):
        try:
            return session.commit(message)
        except icechunk.ConflictError:
            session.rebase(solver)
    return session.commit(message)


def storage_for(location: str | Path):
    """Icechunk storage for a local directory or an ``s3://bucket/prefix`` URL.

    An ingest run on a cloud worker has to leave its repo somewhere that outlives
    the worker, so the repo location may be an S3 prefix as well as a path. S3
    credentials and region come from the environment.
    """
    import icechunk

    text = str(location)
    if not text.startswith("s3://"):
        return icechunk.Storage.new_local_filesystem(str(Path(text).resolve()))
    bucket, _, prefix = text[len("s3://") :].partition("/")
    return icechunk.s3_storage(
        bucket=bucket, prefix=prefix or None, region=s3_region(), from_env=True
    )


def create_repo(
    repo_path: str | Path,
    source_urls: list[str],
    *,
    ideal_request_size: int | None = None,
    open_existing: bool = False,
    prefixes: list[str] | None = None,
):
    """Create an Icechunk repo authorized to read ``source_urls``' bytes.

    ``repo_path`` is a local directory or an ``s3://bucket/prefix`` URL
    (:func:`storage_for`).

    ``prefixes`` names the containers explicitly; by default one is derived per
    source directory. A repo that will hold many studies from one archive wants
    a single broad prefix (the archive's root), so that every study's chunks
    match one container and adding a study never touches the config.

    ``open_existing`` reopens the repo if one is already there, for an ingest that
    adds to a collection over several runs rather than building it in one.

    One :class:`VirtualChunkContainer` per distinct source prefix (local ``file://``,
    ``s3://`` or ``http(s)://``); the repo is self-describing (``open_repo`` reads
    these prefixes back from its config). Shared by :func:`write_manifest_group` and
    :mod:`minus80.core.multiscale`.

    When any source is remote, the repo's storage
    ``ideal_concurrent_request_size`` is set to a large, object-store-appropriate
    value (default :data:`REMOTE_IDEAL_REQUEST_SIZE`; override via
    ``ideal_request_size``) rather than inheriting the local-filesystem default that
    would fragment a remote read into many tiny GETs (icechunk#2256). This is
    persisted in the repo config, so every reader benefits.
    """
    import icechunk

    prefixes = sorted(set(prefixes)) if prefixes else sorted({_url_prefix(u) for u in source_urls})
    remote = any(
        urlparse(p).scheme in ("http", "https", "s3", "gs", "az") for p in prefixes
    )
    config = icechunk.RepositoryConfig.default()
    if (manifest := manifest_config()) is not None:
        config.manifest = manifest
    if remote:
        size = ideal_request_size or REMOTE_IDEAL_REQUEST_SIZE
        config.storage = icechunk.StorageSettings(
            concurrency=icechunk.StorageConcurrencySettings(
                ideal_concurrent_request_size=size
            )
        )
    for prefix in prefixes:
        config.set_virtual_chunk_container(
            icechunk.VirtualChunkContainer(url_prefix=prefix, store=_store_for(prefix))
        )
    storage = storage_for(repo_path)
    if open_existing and icechunk.Repository.exists(storage):
        # A config passed to ``open`` replaces the persisted one for this
        # session only, and ``open_or_create`` ignores it entirely on an
        # existing repo. So an ingest adding a second study to a repo left its
        # new source prefixes unregistered, and every chunk of that study
        # unreadable. Merge into the persisted config and save it, so the repo
        # stays self-describing for readers that know nothing of the ingest.
        persisted = icechunk.Repository.open(storage=storage).config
        if persisted.manifest is not None:
            # Field by field, not a fresh ManifestConfig: a repo's splitting and
            # preload settings are its own and outlive any one ingest.
            try:
                persisted.manifest.max_concurrent_manifest_fetches_during_commit = (
                    MANIFEST_FETCH_CONCURRENCY
                )
            except AttributeError:
                pass
        elif (fresh := manifest_config()) is not None:
            persisted.manifest = fresh
        for prefix in prefixes:
            if prefix not in (persisted.virtual_chunk_containers or {}):
                persisted.set_virtual_chunk_container(
                    icechunk.VirtualChunkContainer(url_prefix=prefix, store=_store_for(prefix))
                )
        all_prefixes = list(persisted.virtual_chunk_containers or {})
        repo = icechunk.Repository.open(
            storage=storage,
            config=persisted,
            authorize_virtual_chunk_access={p: _access_for(p) for p in all_prefixes},
        )
        repo.save_config()
        return repo
    return icechunk.Repository.create(
        storage=storage,
        config=config,
        authorize_virtual_chunk_access={p: _access_for(p) for p in prefixes},
    )


def write_manifest_group(
    group: ManifestGroup,
    repo_path: str | Path,
    *,
    source_urls: list[str],
    message: str = "virtualize",
) -> str:
    """Write the group to a new local Icechunk repo (references only). Returns the
    commit snapshot id. ``source_urls`` are the original files whose bytes the repo
    references; their containing prefixes are authorized for virtual access.

    To write into a session this module did not create (an Arraylake repo, a group
    of an existing store), call ``group.to_icechunk(session.store, group=...)``.
    """
    repo = create_repo(repo_path, source_urls)
    session = repo.writable_session("main")
    group.to_icechunk(session.store)
    return commit(session, message)


def open_repository(repo_path: str | Path, *, source_prefixes: list[str] | None = None):
    """Reopen a persisted Icechunk repo, authorized to follow its virtual chunks.

    ``repo_path`` is a local directory or an ``s3://bucket/prefix`` URL. Returns
    the :class:`icechunk.Repository` itself, for a caller that writes to it or
    picks its own session; :func:`open_repo` is the read-only group on top.
    Self-describing: unless ``source_prefixes`` is given, the prefixes to
    authorize are read back from the repo's own config.
    """
    import icechunk

    storage = storage_for(repo_path)
    if source_prefixes is None:
        containers = (
            icechunk.Repository.open(storage=storage).config.virtual_chunk_containers
            or {}
        )
        source_prefixes = list(containers)
    authorize = (
        {p: _access_for(p) for p in source_prefixes} if source_prefixes else None
    )
    return icechunk.Repository.open(
        storage=storage, authorize_virtual_chunk_access=authorize
    )


def open_repo(
    repo_path: str | Path,
    *,
    branch: str = "main",
    source_prefixes: list[str] | None = None,
    coalesce: bool = False,
    max_gap: int = 262144,
    max_coalesced_bytes: int | None = None,
):
    """Reopen a persisted Icechunk repo as a read-only Zarr group.

    ``branch`` selects which line of the repo to read. Branches are how one store
    carries alternative descriptions of the same chunks -- a second OME-Zarr
    version, say -- without a second copy of the references.

    Self-describing by default: if ``source_prefixes`` is not given, the authorized
    source prefixes are read back from the repo's own config
    (``virtual_chunk_containers``), so a reader needs no prior knowledge of where the
    referenced data lives (GOTCHAS.md "Icechunk virtual-chunk authorization").

    ``coalesce=True`` reads through ``coalescing_zarr`` (the ``bench`` extra): its
    range-coalescing codec pipeline is installed as zarr's default and reads the
    *native* Icechunk session store directly (the pipeline detects a native store and
    merges adjacent chunk byte-ranges into one bulk ``get_many_chunks``; ``max_gap`` is
    the most unwanted bytes tolerated between two chunks before merging, capped by
    ``max_coalesced_bytes``). This is the configuration that makes remote reads fast
    -- a scattered multi-chunk region collapses to a few range GETs instead of one
    per chunk. **Side effect:** it installs the coalescing pipeline process-wide, via
    ``zarr.config`` (the ``max_gap``/``max_coalesced_bytes`` knobs are read when an
    array is first opened, so they are set before the group is opened); use it in a
    read-dedicated process, which is how the shared benchmark runs each read.
    """
    repo = open_repository(repo_path, source_prefixes=source_prefixes)
    session = repo.readonly_session(branch)
    if not coalesce:
        return zarr.open_group(session.store, mode="r")

    import coalescing_zarr as cz
    from coalescing_zarr.config import MAX_COALESCED_BYTES_KEY, MAX_GAP_KEY

    # An array reads these knobs when it is opened, so they are set before open_group.
    zarr.config.set({MAX_GAP_KEY: max_gap, MAX_COALESCED_BYTES_KEY: max_coalesced_bytes})
    cz.register_pipeline()  # left installed for this (read-dedicated) process
    return zarr.open_group(session.store, mode="r")


def ensure_repo_access(client, repo_name: str, *, container_url: str,
                       bucket_nickname: str, vcap_public: bool | None = None) -> None:
    """Make reads of an Arraylake repo's virtual chunks under ``container_url`` resolve.

    Adds the repo's virtual chunk container, mapping ``container_url`` (an
    ``s3://bucket/prefix/`` URL) to the org's bucket config named
    ``bucket_nickname``. Safe to re-run.

    ``vcap_public`` also sets the org-level virtual chunk access policy for that
    bucket config, with that visibility. It needs org-admin rights, and a failure
    is printed and skipped since the policy may already exist. Left as ``None``,
    the policy is not touched: set it beforehand from the Arraylake web app.

    ``client`` is an ``arraylake.Client``. Uses ``update_virtual_chunk_containers``
    where the client has it, else ``authorize_virtual_chunk_access`` (arraylake
    1.2 and earlier).
    """
    if vcap_public is not None:
        org = repo_name.split("/", 1)[0]
        try:
            client.set_virtual_chunk_access_policy(
                org, bucket_nickname=bucket_nickname, subprefix="", public=vcap_public)
        except Exception as e:
            print(f"VCAP: {type(e).__name__}: {e} (continuing; may already exist)")

    mapping = {container_url: bucket_nickname}
    if hasattr(client, "update_virtual_chunk_containers"):
        client.update_virtual_chunk_containers(repo_name, add=mapping)
    else:
        client.authorize_virtual_chunk_access(repo_name, mapping)
    print("virtual chunk containers:", dict(client.get_virtual_chunk_containers(repo_name)))
