"""Object-store registry glue: turn a path/URL into something obstore can read.

Handles local ``file://`` and any cloud scheme obstore understands (``s3://``,
``gs://``, ``az://``, ``http(s)://``).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse

from obspec_utils.registry import ObjectStoreRegistry


def as_url(source: str | Path) -> str:
    """Accept a local path or any URL; return a URL with a scheme.

    A URL passed in is returned as given, percent-encoding and all, because this is
    what gets written into chunk manifests and virtual-chunk-container prefixes and
    read back by clients in other languages. Decoding belongs at the point a URL
    becomes an object-store key -- see :func:`store_and_key` -- not here.
    """
    s = str(source)
    if "://" in s:
        return s
    return Path(s).resolve().as_uri()


def url_dir(url: str) -> str:
    """The containing "directory" of a URL: everything up to the last ``/``.

    Trailing-slashed, because that is the spelling a registry is keyed on.
    """
    return url.rsplit("/", 1)[0] + "/"


def s3_anonymous() -> bool:
    """Whether ``s3://`` sources are read without signing the request.

    A public bucket (the DANDI Archive, say) must be read anonymously, but obstore
    and icechunk sign by default. Opt in with ``VIRTUAL_CORE_S3_ANONYMOUS=1``;
    otherwise credentials come from the environment.
    """
    return os.environ.get("VIRTUAL_CORE_S3_ANONYMOUS", "").lower() in ("1", "true", "yes")


def s3_region() -> str | None:
    """The S3 region from the environment, or None to leave it to the backend."""
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def store_and_key(url: str) -> tuple:
    """Resolve ``url`` to the obstore store holding it and the key within that store.

    This is the one place a URL turns into an object-store key, and so the one
    place percent-encoding is undone: obstore encodes whatever key it is given,
    so a key still reading ``%20`` goes out as ``%2520`` and 404s. Decoding here
    rather than at each call site means a caller may pass either spelling -- the
    href a directory listing gave them, or a name a human typed -- and neither
    can produce that failure, while the URL kept in the manifest stays valid.
    """
    url = as_url(url)
    store, key = _registry_for_dir(url_dir(url)).resolve(url)
    return store, unquote(key)


def registry_for(url: str) -> ObjectStoreRegistry:
    """Build an ObjectStoreRegistry that can read the file (and its chunks).

    For cloud stores needing credentials, build your own registry and pass it in
    to the parser instead of calling this.
    """
    return _build_registry(url_dir(as_url(url)))


@lru_cache(maxsize=64)
def _registry_for_dir(directory: str) -> ObjectStoreRegistry:
    """One registry -- and so one connection pool -- per source directory.

    Every file of a study shares a directory, and building a registry per file
    builds an HTTP client per file: a fresh TLS handshake on each, and as many
    independent pools as there are threads, which defeats any attempt to stay
    under a server's connection limit.
    """
    return _build_registry(directory)


def _build_registry(directory: str) -> ObjectStoreRegistry:
    """A registry holding one store for ``directory`` (a URL ending in ``/``)."""
    from obstore.store import LocalStore, from_url

    parsed = urlparse(directory)
    registry = ObjectStoreRegistry()
    if parsed.scheme in ("", "file"):
        # Register the prefix in the same spelling callers resolve with (the URL as
        # given), while the store itself is rooted at the real on-disk path.
        # Path.as_uri() would re-encode and then never match a URL carrying a
        # literal space.
        registry.register(directory, LocalStore(prefix=Path(unquote(parsed.path))))
        return registry

    opts: dict = {}
    if parsed.scheme == "s3":
        cfg = {}
        if region := s3_region():
            cfg["region"] = region
        if s3_anonymous():
            cfg["skip_signature"] = True
        if cfg:
            opts["config"] = cfg
    elif directory.startswith("http://"):
        # allow_http admits plain-http origins: a localhost benchmark server, an
        # internal cache.
        opts["client_options"] = {"allow_http": True}
    registry.register(directory, from_url(directory, **opts))
    return registry
