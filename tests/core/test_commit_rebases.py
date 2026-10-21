"""A commit that lost the race to the branch tip must still land.

An ingest holds a writable session while it indexes a file, which can take
minutes. Anything else committing to the same branch in that window -- a second
ingest, a metadata restatement -- moves the tip, and the ingest's own commit
then fails outright. These tests provoke exactly that and assert both sides'
work survives.
"""

from __future__ import annotations

import icechunk
import numpy as np
import pytest
import zarr
from minus80.core.icechunk_io import commit


def _repo(path):
    """A repo whose root group is already committed, as a store being added to is.

    A group node created independently by two sessions is a conflict no solver
    resolves, so a store both writers are adding *into* is the case under test.
    """
    repo = icechunk.Repository.create(
        storage=icechunk.Storage.new_local_filesystem(str(path))
    )
    session = repo.writable_session("main")
    zarr.open_group(session.store, mode="a")
    session.commit("the collection root")
    return repo


def _write(session, name, value):
    zarr.open_group(session.store, mode="a").require_array(
        name=name, shape=(1,), dtype="int64", fill_value=0
    )[0] = value


def test_a_commit_behind_the_tip_rebases_onto_it(tmp_path):
    repo = _repo(tmp_path / "repo")
    slow = repo.writable_session("main")
    _write(slow, "slow", 1)
    control = repo.writable_session("main")
    _write(control, "control", 3)

    fast = repo.writable_session("main")
    _write(fast, "fast", 2)
    fast.commit("the other writer got there first")

    # What the driver used to do, and why it needed fixing.
    with pytest.raises(icechunk.ConflictError):
        control.commit("a plain commit from behind the tip")

    assert commit(slow, "the ingest's own commit")

    group = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert group["slow"][0] == 1 and group["fast"][0] == 2


def test_repeated_conflicts_all_rebase(tmp_path):
    """Several writers committing during one session, not just one."""
    repo = _repo(tmp_path / "repo")
    slow = repo.writable_session("main")
    _write(slow, "slow", 1)
    for i in range(5):
        other = repo.writable_session("main")
        _write(other, f"other{i}", i)
        other.commit(f"writer {i}")

    commit(slow, "the ingest's own commit")

    group = zarr.open_group(repo.readonly_session("main").store, mode="r")
    assert group["slow"][0] == 1
    assert [group[f"other{i}"][0] for i in range(5)] == list(range(5))


def test_the_same_array_written_twice_still_refuses(tmp_path):
    """The solver resolves disjoint writes, not two descriptions of one array."""
    repo = _repo(tmp_path / "repo")
    slow = repo.writable_session("main")
    zarr.open_group(slow.store, mode="a").require_array(
        name="shared", shape=(4,), dtype="int64", fill_value=0
    )
    other = repo.writable_session("main")
    zarr.open_group(other.store, mode="a").require_array(
        name="shared", shape=(8,), dtype=np.dtype("float32"), fill_value=0
    )
    other.commit("the other writer's version of the array")

    with pytest.raises(icechunk.RebaseFailedError):
        commit(slow, "the ingest's own commit")
