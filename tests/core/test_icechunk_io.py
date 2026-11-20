import icechunk
import pytest

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


def test_pinned_references_refuse_a_changed_source(tmp_path):
    import datetime

    import numpy as np
    import zarr
    from virtualizarr.manifests import ManifestGroup

    from minus80.core.builders.flat_binary import flat_manifest_array
    from minus80.core.icechunk_io import commit, create_repo, open_repository, pin_virtual_refs
    from minus80.core.registry import as_url

    source = tmp_path / "data" / "column.npy"
    source.parent.mkdir()
    np.save(source, np.arange(8, dtype="<f8"))
    url = as_url(source)
    array = flat_manifest_array(url, shape=(8,), dtype="float64", data_offset=128,
                                endian="little", rows_per_chunk=4, dimension_names=("row",))
    repo = create_repo(tmp_path / "repo", [url])
    session = repo.writable_session("main")
    ManifestGroup(arrays={"column": array}).to_icechunk(session.store)
    before = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)
    pin_virtual_refs(session.store, "column", array, before)
    commit(session, "pinned")
    read = lambda: zarr.open_array(open_repository(tmp_path / "repo").readonly_session("main").store,
                                   path="column", mode="r")[:]
    assert np.array_equal(read(), np.arange(8))

    session = repo.writable_session("main")
    long_ago = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    pin_virtual_refs(session.store, "column", array, long_ago)
    commit(session, "pinned to a time before the file was written")
    with pytest.raises(Exception):
        read()
