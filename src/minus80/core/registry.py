"""Object-store registry glue: turn a path/URL into something obstore can read.

Handles local ``file://`` and any cloud scheme obstore understands (``s3://``,
``gs://``, ``az://``, ``http(s)://``).
"""
from __future__ import annotations

import datetime
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from obspec_utils.registry import ObjectStoreRegistry

#: obstore builds an HTTP store on object_store's defaults, which abandon a
#: request after three minutes. An index walk is thousands of sequential range
#: reads against one host over tens of minutes, so a request the library gives
#: up on throws away the whole walk rather than one read.
CLIENT_OPTIONS = {"timeout": "300s", "connect_timeout": "60s"}

#: The backoff is set rather than left at the library's, which starts at
#: 100 ms: against a server refusing connections outright, twenty retries on
#: that ladder are spent in eleven seconds and the walk dies anyway. Starting
#: at two seconds and climbing to a minute spends the same retries over a
#: quarter of an hour, which is the timescale a public archive recovers on.
RETRY_CONFIG = {
    "max_retries": 30,
    "retry_timeout": datetime.timedelta(minutes=20),
    "backoff": {"init_backoff": datetime.timedelta(seconds=2),
                "max_backoff": datetime.timedelta(seconds=60),
                "base": 2.0},
}


def as_url(source: str | Path) -> str:
    """Accept a local path or any URL; return a URL with a scheme.

    A URL passed in is returned as given, percent-encoding and all, because this is
    what gets written into chunk manifests and virtual-chunk-container prefixes and
    read back by clients in other languages. Decoding happens where a URL
    becomes an object-store key, in :func:`store_and_key`.
    """
    s = str(source)
    if "://" in s:
        return s
    return Path(s).resolve().as_uri()


def file_path(url: str | Path) -> Path | None:
    """The local path a ``file://`` URL or bare path names, percent-decoded; None for a remote URL."""
    s = str(url)
    if s.startswith("file://"):
        return Path(url2pathname(urlparse(s).path))
    if "://" in s:
        return None
    return Path(s)


def url_dir(url: str) -> str:
    """The containing "directory" of a URL: everything up to the last ``/``.

    Trailing-slashed, because that is the spelling a registry is keyed on.
    """
    return url.rsplit("/", 1)[0] + "/"


def s3_anonymous() -> bool:
    """Whether ``s3://`` sources are read without signing the request.

    A public bucket (the DANDI Archive, say) must be read anonymously, but obstore
    and icechunk sign by default. Opt in with ``MINUS80_S3_ANONYMOUS=1``;
    otherwise credentials come from the environment.
    """
    return os.environ.get("MINUS80_S3_ANONYMOUS", "").lower() in ("1", "true", "yes")


def s3_region() -> str | None:
    """The S3 region from the environment, or None to leave it to the backend."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def store_and_key(url: str, *, patient: bool = False) -> tuple:
    """Resolve ``url`` to the obstore store holding it and the key within that store.

    This is the one place a URL turns into an object-store key, and so the one
    place percent-encoding is undone: obstore encodes whatever key it is given,
    so a key still reading ``%20`` goes out as ``%2520`` and 404s. Decoding here
    rather than at each call site means a caller may pass either spelling -- the
    href a directory listing gave them, or a name a human typed -- and neither
    can produce that failure, while the URL kept in the manifest stays valid.

    ``patient`` gives an HTTP store :data:`CLIENT_OPTIONS` and
    :data:`RETRY_CONFIG`, for a walk over a file's index that must outlast a
    slow or briefly refusing server. Without it the store keeps object_store's
    defaults, which fail fast.
    """
    url = as_url(url)
    if (path := file_path(url)) is not None:
        # ObjectStoreRegistry.resolve strips the store's decoded prefix from the
        # URL's still-encoded path, so in a directory whose name holds a space
        # or a "+" nothing is stripped and the key is the whole path
        # (docs/upstream.md). A store rooted at the decoded directory, keyed on
        # the decoded name, never meets that comparison.
        return _local_store(str(path.parent)), path.name
    if url.startswith(("http://", "https://")) and urlparse(url).query:
        # The query of a signed or tokened link is part of the object's address,
        # and a store keyed on the directory drops it. A store rooted at the
        # whole URL, read with an empty key, sends it.
        return _store_for_url(url, patient), ""
    store, key = _registry_for_dir(url_dir(url), patient).resolve(url)
    return store, unquote(key)


@lru_cache(maxsize=64)
def _local_store(directory: str):
    from obstore.store import LocalStore

    return LocalStore(prefix=directory)


@lru_cache(maxsize=64)
def _store_for_url(url: str, patient: bool = False):
    from obstore.store import from_url

    return from_url(url, **_http_options(url, patient))


def _http_options(url: str, patient: bool) -> dict:
    """``from_url`` keywords for an HTTP store at ``url``."""
    client = dict(CLIENT_OPTIONS) if patient else {}
    if url.startswith("http://"):
        # allow_http admits plain-http origins: a localhost benchmark server, an
        # internal cache.
        client["allow_http"] = True
    opts: dict = {"client_options": client} if client else {}
    if patient:
        opts["retry_config"] = RETRY_CONFIG
    return opts


def registry_for(url: str, *, patient: bool = False) -> ObjectStoreRegistry:
    """Build an ObjectStoreRegistry that can read the file (and its chunks).

    ``patient`` is as for :func:`store_and_key`. For cloud stores needing
    credentials, build your own registry and pass it in to the parser instead
    of calling this.
    """
    return _build_registry(url_dir(as_url(url)), patient)


@lru_cache(maxsize=64)
def _registry_for_dir(directory: str, patient: bool = False) -> ObjectStoreRegistry:
    """One registry, and so one connection pool, per source directory.

    Every file of a study shares a directory, and building a registry per file
    builds an HTTP client per file: a fresh TLS handshake on each, and as many
    independent pools as there are threads, which defeats any attempt to stay
    under a server's connection limit.
    """
    return _build_registry(directory, patient)


def _build_registry(directory: str, patient: bool = False) -> ObjectStoreRegistry:
    """A registry holding one store for ``directory`` (a URL ending in ``/``)."""
    from obstore.store import LocalStore, from_url

    registry = ObjectStoreRegistry()
    if (path := file_path(directory)) is not None:
        # Register the prefix in the same spelling callers resolve with (the URL as
        # given), while the store itself is rooted at the real on-disk path.
        # Path.as_uri() would re-encode and then never match a URL carrying a
        # literal space.
        registry.register(directory, LocalStore(prefix=path))
        return registry

    scheme = urlparse(directory).scheme
    root, opts = directory, {}
    if scheme == "s3":
        cfg = {}
        if region := s3_region():
            cfg["region"] = region
        if s3_anonymous():
            cfg["skip_signature"] = True
        if cfg:
            opts["config"] = cfg
    elif scheme in ("http", "https"):
        opts = _http_options(directory, patient)
        # An HTTP store joins its root and a key with "/", so a root that ends
        # in one requests "dir//name", which a strict server answers with 404.
        root = directory.rstrip("/")
    registry.register(directory, from_url(root, **opts))
    return registry
