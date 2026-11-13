"""Create, write and reopen Icechunk repos of virtual chunk references.

A repo gets one :class:`icechunk.VirtualChunkContainer` per source prefix, so
its references resolve back into the original files, and every container's
prefix is passed to ``authorize_virtual_chunk_access``, without which Icechunk
refuses to read virtual chunks. The containers are saved in the repo's config,
so :func:`open_repo` can authorize them again without being told the sources.

Local, ``s3://``, ``gs://`` and ``http(s)://`` sources are supported. Any other scheme
raises ``ValueError``.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import zarr
from virtualizarr.manifests import ManifestGroup

from .registry import file_path, s3_anonymous, s3_region, url_dir


def _url_prefix(url: str) -> str:
    """The virtual-chunk-container prefix (the source's containing "directory").

    Local paths resolve to a ``file://`` directory URI; ``http(s)`` URLs keep their
    scheme/host and drop the final path segment. One prefix groups every source that
    shares it into a single container.
    """
    if (path := file_path(url)) is not None:
        # Decoded to match registry_for, which decodes too; otherwise a
        # directory with a space in it re-encodes to %2520 and never matches.
        return path.resolve().parent.as_uri() + "/"
    return url_dir(url)


def _access_for(prefix: str):
    """The credential ``authorize_virtual_chunk_access`` takes for a source prefix.

    Local sources get ``LocalFileSystemAccess``. ``s3://`` gets anonymous or
    environment credentials (see :func:`minus80.core.registry.s3_anonymous`).
    ``gs://`` gets anonymous credentials: the Google Cloud buckets the ingests
    reference are public archives. ``http(s)`` gets ``HttpAccess``, which is
    anonymous; the container's store carries the connection settings.
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
    if scheme == "gs":
        return icechunk.credentials.Credentials.Gcs(
            icechunk.credentials.gcs_anonymous_credentials()
        )
    return icechunk.credentials.HttpAccess


def _store_for(prefix: str):
    """The Icechunk object store backing a container prefix.

    ``s3://`` uses ``s3_store`` (the bucket's region and anonymity, see
    :func:`minus80.core.registry.s3_region`), so a repo of references into a public S3
    bucket resolves without local credentials. ``gs://`` uses an unsigned
    ``gcs_store``, matching the anonymous credentials. Arraylake accepts S3-backed
    containers and refuses HTTP-backed ones. ``http(s)`` uses ``http_store``
    with ``allow_http``, so plain-http origins such as a localhost server work
    as well as TLS ones.
    """
    import icechunk

    scheme = urlparse(prefix).scheme
    if (path := file_path(prefix)) is not None:
        return icechunk.local_filesystem_store(str(path))
    if scheme == "s3":
        return icechunk.s3_store(
            region=s3_region(urlparse(prefix).netloc), anonymous=s3_anonymous()
        )
    if scheme == "gs":
        return icechunk.gcs_store({"google_skip_signature": "true"})
    if scheme in ("http", "https"):
        return icechunk.http_store({"allow_http": "true"})
    raise ValueError(
        f"no built-in virtual-chunk store for scheme {scheme!r}; pass a "
        "pre-configured registry/store for other cloud backends."
    )


# A repo stored on local disk defaults its ideal request size to 4 KiB, and a
# virtual read from a remote source inherits it, splitting one multi-chunk read
# into dozens of latency-bound GETs (icechunk#2256). Any repo with a remote
# source gets this size instead, so reads coalesce into a few ~12 MiB requests.
REMOTE_IDEAL_REQUEST_SIZE = 12 * 1024 * 1024

# The same inheritance gives virtual chunk reads the local disk's single try,
# so one dropped connection on any of an array's hundreds of GETs fails the
# whole read ("connection closed before message completed", icechunk#1431).
# These are the attempts a remote read gets, with backoff in milliseconds.
REMOTE_READ_TRIES = 10
REMOTE_READ_BACKOFF_MS = (100, 2000)


def _remote_retries():
    import icechunk

    first, most = REMOTE_READ_BACKOFF_MS
    return icechunk.StorageRetriesSettings(
        max_tries=REMOTE_READ_TRIES, initial_backoff_ms=first, max_backoff_ms=most
    )


def _with_remote_retries(config):
    """``config`` with :data:`REMOTE_READ_TRIES`, unless it already allows more."""
    import icechunk

    storage = config.storage or icechunk.StorageSettings()
    retries = storage.retries
    if retries is None or (retries.max_tries or 1) < REMOTE_READ_TRIES:
        storage.retries = _remote_retries()
    config.storage = storage
    return config


#: Icechunk fetches manifests one at a time while committing unless told otherwise,
#: so a commit costs one round trip per array: 200 arrays at 50 ms latency took
#: 11.45 s that way and 0.83 s with concurrent fetches. No caller wants the
#: default of 1, so it is set here rather than offered as an argument.
MANIFEST_FETCH_CONCURRENCY = 32


#: How many times a commit may rebase onto whatever landed on the branch first.
#: A long ingest commits once per source file and holds its session across a
#: whole file's indexing, so another writer -- a second ingest, a metadata
#: restatement -- has a wide window to commit first.
COMMIT_REBASE_TRIES = 100


def commit(
    session, message: str, *, metadata: dict | None = None, tries: int = COMMIT_REBASE_TRIES
) -> str:
    """Commit a writable session, rebasing onto concurrent commits on its branch.

    A commit from a session that no longer sits at the branch tip fails outright.
    Icechunk's own conflict solver replays this session's changes on top of the
    newer ancestry, which resolves the case every ingest hits: two writers
    touching different nodes of the same store. A conflict the solver cannot
    resolve, such as the same array written twice differently, still raises
    ``RebaseFailedError``, because picking a winner there is the caller's call.

    ``metadata`` is stored on the snapshot beside the message, so a reader of the
    history can tell what a commit changed without diffing it.
    """
    import icechunk

    return session.commit(
        message, metadata=metadata, rebase_with=icechunk.BasicConflictSolver(), rebase_tries=tries
    )


def storage_for(location: str | Path):
    """Icechunk storage for a local directory or an ``s3://bucket/prefix`` URL.

    An ingest run on a cloud worker has to leave its repo somewhere that outlives
    the worker, so the repo location may be an S3 prefix as well as a path. S3
    credentials come from the environment, the region as for
    :func:`minus80.core.registry.s3_region`.
    """
    import icechunk

    text = str(location)
    if not text.startswith("s3://"):
        return icechunk.Storage.new_local_filesystem(str(Path(text).resolve()))
    bucket, _, prefix = text[len("s3://") :].partition("/")
    return icechunk.s3_storage(
        bucket=bucket, prefix=prefix or None, region=s3_region(bucket), from_env=True
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
    many small GETs (icechunk#2256), and its retries to
    :data:`REMOTE_READ_TRIES` instead of the local disk's single try. Both are
    saved in the repo config, so they apply to every reader. Reopening with
    ``open_existing`` does not change the settings of an existing repo.
    """
    import icechunk

    prefixes = sorted(set(prefixes)) if prefixes else sorted({_url_prefix(u) for u in source_urls})
    remote = any(
        urlparse(p).scheme in ("http", "https", "s3", "gs", "az") for p in prefixes
    )
    config = icechunk.RepositoryConfig.default()
    config.manifest = icechunk.ManifestConfig(
        max_concurrent_manifest_fetches_during_commit=MANIFEST_FETCH_CONCURRENCY
    )
    if remote:
        size = ideal_request_size or REMOTE_IDEAL_REQUEST_SIZE
        config.storage = icechunk.StorageSettings(
            concurrency=icechunk.StorageConcurrencySettings(
                ideal_concurrent_request_size=size
            ),
            retries=_remote_retries(),
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
            persisted.manifest.max_concurrent_manifest_fetches_during_commit = (
                MANIFEST_FETCH_CONCURRENCY
            )
        else:
            persisted.manifest = icechunk.ManifestConfig(
                max_concurrent_manifest_fetches_during_commit=MANIFEST_FETCH_CONCURRENCY
            )
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
    from the repo's own config. When any is remote, the session reads with at
    least :data:`REMOTE_READ_TRIES` tries, whatever the stored config says.
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
    config = None
    if any(urlparse(p).scheme in ("http", "https", "s3", "gs", "az") for p in source_prefixes or []):
        # A repo written before its config carried retries still reads with
        # one try; the override lasts for this session and changes nothing
        # stored.
        config = _with_remote_retries(icechunk.Repository.open(storage=storage).config)
    return icechunk.Repository.open(
        storage=storage, config=config, authorize_virtual_chunk_access=authorize
    )


def open_repo(
    repo_path: str | Path,
    *,
    branch: str = "main",
    source_prefixes: list[str] | None = None,
):
    """Reopen a persisted Icechunk repo as a read-only Zarr group.

    ``branch`` selects which line of the repo to read. Branches are how one store
    carries alternative descriptions of the same chunks -- a second OME-Zarr
    version, say -- without a second copy of the references.

    Unless ``source_prefixes`` is given, the prefixes to authorize are read
    from the repo's ``virtual_chunk_containers`` config, so the caller does not
    need to know where the referenced data lives (docs/ingest.md).
    """
    repo = open_repository(repo_path, source_prefixes=source_prefixes)
    session = repo.readonly_session(branch)
    return zarr.open_group(session.store, mode="r")


#: A repo location of this form, ``arraylake://<org>/<repo>``, names an
#: existing Arraylake repo instead of a storage location.
ARRAYLAKE = "arraylake://"


class UncoveredSourceError(ValueError):
    """A source prefix falls under none of the repo's virtual chunk containers.

    The text names neither the prefixes nor the containers, so a log does not show a storage location;
    ``uncovered`` and ``containers`` hold them.
    """

    def __init__(self, repo: str, uncovered: list[str], containers: list[str], asked: int):
        self.uncovered, self.containers = uncovered, containers
        super().__init__(
            f"{repo} has {len(containers)} virtual chunk container(s), and no virtual chunk container covers "
            f"{len(uncovered)} of the {asked} source prefixes that the caller names. An org admin registers the "
            f"source bucket as a bucket config and gives it a virtual chunk access policy; a repo writer then adds "
            f"the container with Client.update_virtual_chunk_containers.")


def arraylake_repository(location: str, *, source_prefixes: list[str] | None = None):
    """Open the Arraylake repo ``location`` names (``arraylake://<org>/<repo>``).

    Arraylake owns the repo's storage and its virtual chunk containers, and
    applies the containers itself when the repo opens, so nothing is passed
    beyond the name: a reader that adds containers of its own is refused.
    Each of ``source_prefixes`` must fall under one of the repo's containers,
    or the references written would not be readable; that raises
    ``UncoveredSourceError`` (a ``ValueError``) naming the setup the repo lacks. The session commits with
    :data:`MANIFEST_FETCH_CONCURRENCY` and reads with at least
    :data:`REMOTE_READ_TRIES`, as a repo from :func:`create_repo` does. Needs the
    ``arraylake`` package and a login (``al auth login``) or ``ARRAYLAKE_TOKEN``.
    """
    from arraylake import Client

    name = location.removeprefix(ARRAYLAKE)
    client = Client()
    containers = dict(client.get_virtual_chunk_containers(name))
    def under(prefix: str, container: str) -> bool:
        return f"{prefix.rstrip('/')}/".startswith(f"{container.rstrip('/')}/")

    uncovered = [p for p in source_prefixes or []
                 if not any(under(p, c) for c in containers)]
    if uncovered:
        raise UncoveredSourceError(name, uncovered, sorted(containers), len(source_prefixes))
    # The repo's stored config is Arraylake's, which leaves the commit's
    # manifest concurrency at icechunk's default of 1: a commit of thousands
    # of arrays then writes them one round trip at a time. get_repo merges
    # this override over the stored config for the session. Reopening the
    # repo instead would drop the commit author get_repo sets.
    import icechunk

    config = _with_remote_retries(icechunk.RepositoryConfig())
    config.manifest = icechunk.ManifestConfig()
    config.manifest.max_concurrent_manifest_fetches_during_commit = MANIFEST_FETCH_CONCURRENCY
    return client.get_repo(name, config=config)
