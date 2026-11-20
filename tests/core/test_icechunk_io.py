import icechunk

from minus80.core import icechunk_io
from minus80.core.icechunk_io import REMOTE_READ_TRIES, create_repo, open_repository

REMOTE = "s3://czb-opencell/microscopy/raw/"


def _tries(repo) -> int:
    """The tries a repo's reads get, after icechunk merges in its storage's defaults."""
    return repo.config.storage.retries.max_tries


def test_a_repo_with_a_remote_source_stores_retries(tmp_path):
    create_repo(tmp_path / "repo", [], prefixes=[REMOTE])
    stored = icechunk.Repository.open(storage=icechunk_io.storage_for(tmp_path / "repo"))
    assert _tries(stored) == REMOTE_READ_TRIES


def test_reopening_an_older_repo_reads_with_retries(tmp_path):
    # A repo written before create_repo stored retries: its config carries
    # the remote container and nothing else, so reads get the local disk's
    # single try.
    config = icechunk.RepositoryConfig.default()
    config.set_virtual_chunk_container(
        icechunk.VirtualChunkContainer(url_prefix=REMOTE, store=icechunk_io._store_for(REMOTE)))
    storage = icechunk_io.storage_for(tmp_path / "repo")
    icechunk.Repository.create(storage=storage, config=config)
    assert _tries(icechunk.Repository.open(storage=storage)) == 1

    assert _tries(open_repository(tmp_path / "repo")) == REMOTE_READ_TRIES
    assert _tries(icechunk.Repository.open(storage=storage)) == 1


def test_a_gcs_source_gets_an_anonymous_container(tmp_path):
    prefix = "gs://noaa-passive-bioacoustic/"
    create_repo(tmp_path / "repo", [], prefixes=[prefix])
    repo = open_repository(tmp_path / "repo")
    assert list(repo.config.virtual_chunk_containers) == [prefix]
    assert _tries(repo) == REMOTE_READ_TRIES


def test_more_tries_than_the_floor_are_kept(tmp_path):
    config = icechunk.RepositoryConfig.default()
    config.storage = icechunk.StorageSettings(retries=icechunk.StorageRetriesSettings(max_tries=50))
    assert icechunk_io._with_remote_retries(config).storage.retries.max_tries == 50


def test_a_local_only_repo_is_left_alone(tmp_path):
    source = tmp_path / "data.bin"
    source.write_bytes(b"x")
    create_repo(tmp_path / "repo", [source.as_uri()])
    assert _tries(open_repository(tmp_path / "repo")) == 1


def test_an_arraylake_repo_must_cover_the_sources(monkeypatch):
    import sys
    import types

    import pytest

    class Repo:
        def __init__(self, config):
            self.config = config

    class Client:
        def get_virtual_chunk_containers(self, name):
            return {"s3://czb-opencell": "czb-opencell"}

        def get_repo(self, name, config=None):
            return Repo(config)

    monkeypatch.setitem(sys.modules, "arraylake", types.SimpleNamespace(Client=Client))
    location = "arraylake://org/opencell"
    config = icechunk_io.arraylake_repository(location, source_prefixes=[REMOTE]).config
    assert config.manifest.max_concurrent_manifest_fetches_during_commit == icechunk_io.MANIFEST_FETCH_CONCURRENCY
    assert config.storage.retries.max_tries == REMOTE_READ_TRIES
    with pytest.raises(ValueError, match="no virtual chunk container covering"):
        icechunk_io.arraylake_repository(location, source_prefixes=["s3://other-bucket/"])


def test_write_pinned_refuses_a_rewritten_source(tmp_path, range_server):
    """A reference pinned to its object's ETag reads while the ETag holds, and is
    refused once the server reports another, as for a source rewritten in place."""
    import numpy as np
    import pytest
    import zarr
    from virtualizarr.manifests import ManifestGroup

    from minus80.core.builders.flat_binary import flat_manifest_array
    from minus80.core.icechunk_io import create_repo, write_pinned

    values = np.arange(12, dtype="<u2")
    range_server.files["/data.bin"] = values.tobytes()
    range_server.etags["/data.bin"] = '"one"'
    url = f"{range_server.url}/data.bin"
    array = flat_manifest_array(url, shape=(3, 4), dtype="uint16", data_offset=0, endian="little")
    repo = create_repo(tmp_path / "repo", [url])
    session = repo.writable_session("main")
    with pytest.raises(KeyError):
        write_pinned(session.store, "g", ManifestGroup(arrays={"a": array}), {})
    write_pinned(session.store, "g", ManifestGroup(arrays={"a": array}), {url: '"one"'})
    session.commit("pinned")
    read = lambda: zarr.open_array(repo.readonly_session("main").store, path="g/a", mode="r")[:]  # noqa: E731
    np.testing.assert_array_equal(read(), values.reshape(3, 4))
    range_server.etags["/data.bin"] = '"two"'
    with pytest.raises(Exception, match="checksum of the object owning the virtual chunk has changed"):
        read()
