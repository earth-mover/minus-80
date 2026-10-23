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


def test_more_tries_than_the_floor_are_kept(tmp_path):
    config = icechunk.RepositoryConfig.default()
    config.storage = icechunk.StorageSettings(retries=icechunk.StorageRetriesSettings(max_tries=50))
    assert icechunk_io._with_remote_retries(config).storage.retries.max_tries == 50


def test_a_local_only_repo_is_left_alone(tmp_path):
    source = tmp_path / "data.bin"
    source.write_bytes(b"x")
    create_repo(tmp_path / "repo", [source.as_uri()])
    assert _tries(open_repository(tmp_path / "repo")) == 1
