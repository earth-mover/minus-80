"""A commit that lost the race to the branch tip must still land.

An ingest holds a writable session while it indexes a file, which can take
minutes. Anything else committing to the same branch in that window -- a second
ingest, a metadata restatement -- moves the tip, and the ingest's own commit
then fails outright. These tests provoke exactly that and assert both sides'
work survives.
"""

import icechunk
import pytest
import zarr

from minus80.core.icechunk_io import commit


def _repo(path):
    # Two sessions creating the same group conflict unresolvably, so both
    # writers add into an already committed root, as an ingest does.
    repo = icechunk.Repository.create(storage=icechunk.Storage.new_local_filesystem(str(path)))
    session = repo.writable_session("main")
    zarr.open_group(session.store, mode="a")
    session.commit("root")
    return repo


def _write(session, name, value, shape=(1,), dtype="int64"):
    zarr.open_group(session.store, mode="a").require_array(
        name=name, shape=shape, dtype=dtype, fill_value=0
    )[0] = value


def test_commit_behind_the_tip_rebases(tmp_path):
    repo = _repo(tmp_path / "repo")
    slow = repo.writable_session("main")
    _write(slow, "slow", 1)
    for i in range(3):
        other = repo.writable_session("main")
        _write(other, f"other{i}", i)
        other.commit(f"writer {i}")

    commit(slow, "ingest")

    group = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert group["slow"][0] == 1
    assert [group[f"other{i}"][0] for i in range(3)] == [0, 1, 2]


def test_same_array_written_twice_refuses(tmp_path):
    repo = _repo(tmp_path / "repo")
    slow = repo.writable_session("main")
    _write(slow, "shared", 1, shape=(4,))
    other = repo.writable_session("main")
    _write(other, "shared", 2, shape=(8,), dtype="float32")
    other.commit("other writer")

    with pytest.raises(icechunk.RebaseFailedError):
        commit(slow, "ingest")


def test_metadata_survives_the_rebase(tmp_path):
    repo = _repo(tmp_path / "repo")
    slow = repo.writable_session("main")
    _write(slow, "slow", 1)
    other = repo.writable_session("main")
    _write(other, "other", 2)
    other.commit("other writer")

    commit(slow, "ingest", metadata={"change": {"decision": 17, "chunks_written": 0}})

    latest = next(iter(repo.ancestry(branch="main")))
    assert latest.metadata["change"] == {"decision": 17, "chunks_written": 0}
