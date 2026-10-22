"""Create, write and reopen Icechunk repos of virtual chunk references.

A repo gets one :class:`icechunk.VirtualChunkContainer` per source prefix, so
its references resolve back into the original files, and every container's
prefix is passed to ``authorize_virtual_chunk_access``, without which Icechunk
refuses to read virtual chunks. The containers are saved in the repo's config,
so :func:`open_repo` can authorize them again without being told the sources.

Local, ``s3://`` and ``http(s)://`` sources are supported. Any other scheme
raises ``ValueError``.
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
        # Unquoted to match registry_for, which unquotes too; otherwise a
        # directory with a space in it re-encodes to %2520 and never matches.
        return Path(unquote(urlparse(url).path)).resolve().parent.as_uri() + "/"
    return url_dir(url)


def _access_for(prefix: str):
    """The credential ``authorize_virtual_chunk_access`` takes for a source prefix.

    Local sources get ``LocalFileSystemAccess``. ``s3://`` gets anonymous or
    environment credentials (see :func:`minus80.core.registry.s3_anonymous`).
    ``http(s)`` gets ``HttpAccess``, which is anonymous; the container's store
    carries the connection settings.
    """
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
        return icechunk.credentials.Credentials.S3(cred)
    return icechunk.credentials.HttpAccess


def _store_for(prefix: str):
    """The Icechunk object store backing a container prefix.

    ``s3://`` uses ``s3_store`` (region and anonymity from the environment, see
    :mod:`minus80.core.registry`), so a repo of references into a public S3
    bucket resolves without local credentials. Arraylake accepts S3-backed
    containers and refuses HTTP-backed ones. ``http(s)`` uses ``http_store``
    with ``allow_http``, so plain-http origins such as a localhost server work
    as well as TLS ones.
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


# A repo stored on local disk defaults its ideal request size to 4 KiB, and a
# virtual read from a remote source inherits it, splitting one multi-chunk read
# into dozens of latency-bound GETs (icechunk#2256). Any repo with a remote
# source gets this size instead, so reads coalesce into a few ~12 MiB requests.
REMOTE_IDEAL_REQUEST_SIZE = 12 * 1024 * 1024


#: Icechunk fetches manifests one at a time while committing unless told otherwise,
#: so a commit costs one round trip per array: 200 arrays at 50 ms latency took
#: 11.45 s that way and 0.83 s with concurrent fetches. No caller wants the
#: default of 1, so it is set here rather than offered as an argument.
MANIFEST_FETCH_CONCURRENCY = 32


def manifest_config():
    """Icechunk's manifest settings, or None on a release without the concurrency argument.

    Committing serially is slower, not wrong, so an icechunk release without
    ``max_concurrent_manifest_fetches_during_commit`` keeps its default rather
    than failing to build a repo.
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
    resolve, such as the same array written twice differently, still raises
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

    The repo gets one :class:`VirtualChunkContainer` per distinct prefix (local
    ``file://``, ``s3://`` or ``http(s)://``), saved in its config so that
    :func:`open_repo` can read the prefixes back.

    When any prefix is remote, the repo's ``ideal_concurrent_request_size`` is
    set to ``ideal_request_size``, or :data:`REMOTE_IDEAL_REQUEST_SIZE` by
    default, instead of the local-disk default that splits a remote read into
    many small GETs (icechunk#2256). The setting is saved in the repo config,
    so it applies to every reader. Reopening with ``open_existing`` does not
    change the setting of an existing repo.
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
        # session only, and ``open_or_create`` ignores it on an existing repo.
        # Either way a second study's new prefixes would go unregistered and its
        # chunks unreadable. Merging into the persisted config and saving it
        # keeps the containers readable by anyone who opens the repo.
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
    """Write the group to a new Icechunk repo and commit it. Returns the snapshot id.

    ``repo_path`` is a local directory or an ``s3://bucket/prefix`` URL.
    ``source_urls`` are the files whose bytes the group references; their
    containing prefixes become the repo's virtual chunk containers.

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
    Unless ``source_prefixes`` is given, the prefixes to authorize are read
    from the repo's own config.
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

    Unless ``source_prefixes`` is given, the prefixes to authorize are read
    from the repo's ``virtual_chunk_containers`` config, so the caller does not
    need to know where the referenced data lives (docs/ingest.md).

    ``coalesce=True`` reads through ``coalescing_zarr``, which is not a
    dependency of minus-80 and must be installed separately. Its codec pipeline
    becomes zarr's default and merges the byte ranges of nearby chunks into one
    ``get_many_chunks`` call on the Icechunk session store: ``max_gap`` is the
    most unrequested bytes allowed between two chunks it merges, and
    ``max_coalesced_bytes`` caps a merged read. A scattered multi-chunk region
    then takes a few range GETs instead of one per chunk. The pipeline and both
    settings are installed process-wide through ``zarr.config`` and stay
    installed, so use it in a process that only reads.
    """
    repo = open_repository(repo_path, source_prefixes=source_prefixes)
    session = repo.readonly_session(branch)
    if not coalesce:
        return zarr.open_group(session.store, mode="r")

    import coalescing_zarr as cz
    from coalescing_zarr.config import MAX_COALESCED_BYTES_KEY, MAX_GAP_KEY

    # An array reads these knobs when it is opened, so they are set before open_group.
    zarr.config.set({MAX_GAP_KEY: max_gap, MAX_COALESCED_BYTES_KEY: max_coalesced_bytes})
    cz.register_pipeline()
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
