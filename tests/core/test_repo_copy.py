import asyncio

import numpy as np
import pytest
import zarr

from minus80.core.icechunk_io import commit, create_repo, open_repository
from minus80.core.repo_copy import check, copy_units, units_at


@pytest.fixture
def source(tmp_path):
    """A repo with two units: ``a`` holds virtual chunks, ``b`` holds stored ones."""
    data = tmp_path / "files" / "data.bin"
    data.parent.mkdir()
    data.write_bytes(bytes(range(16)))
    prefix = data.parent.as_uri() + "/"
    repo = create_repo(tmp_path / "src", [], prefixes=[prefix])
    session = repo.writable_session("main")
    root = zarr.open_group(session.store, mode="w", zarr_format=3)
    root.attrs["title"] = "source"
    a = root.create_group("a")
    a.attrs["kind"] = "virtual"
    virtual = a.create_array("v", shape=(8,), chunks=(4,), dtype="u1", compressors=None)
    session.store.set_virtual_ref("a/v/c/0", data.as_uri(), offset=0, length=4, validate_container=True)
    session.store.set_virtual_ref("a/v/c/1", data.as_uri(), offset=8, length=4, validate_container=True)
    b = root.create_group("b")
    stored = b.create_array("s", shape=(6,), chunks=(3,), dtype="i2", compressors=None)
    stored[:] = np.arange(6, dtype="i2")
    commit(session, "source")
    return repo, prefix, data


def open_source(repo):
    return repo.readonly_session("main").store


def run(coro):
    return asyncio.run(coro)


def copy_all(repo, prefix, tmp_path):
    src = open_source(repo)
    target = create_repo(tmp_path / "dst", [], prefixes=[prefix])
    parents, units = run(units_at(src, 1))
    run(copy_units(src, target, parents, units, "test source"))
    return src, target


def test_units_are_the_nodes_at_the_depth(source):
    repo, _, _ = source
    parents, units = run(units_at(open_source(repo), 1))
    assert parents == [""]
    assert units == ["a", "b"]


def test_a_copy_matches_its_source(source, tmp_path):
    repo, prefix, data = source
    src, target = copy_all(repo, prefix, tmp_path)
    dst = open_repository(tmp_path / "dst").readonly_session("main").store
    for unit in ("a", "b"):
        run(check(src, dst, unit, sample=10))

    root = zarr.open_group(dst, mode="r")
    assert root.attrs["title"] == "source"
    assert root["a"].attrs["kind"] == "virtual"
    raw = data.read_bytes()
    assert bytes(root["a/v"][:]) == raw[0:4] + raw[8:12]
    assert root["b/s"][:].tolist() == list(range(6))


def test_a_second_run_skips_what_is_already_copied(source, tmp_path, capsys):
    repo, prefix, _ = source
    src, target = copy_all(repo, prefix, tmp_path)
    before = len(list(target.ancestry(branch="main")))
    parents, units = run(units_at(src, 1))
    run(copy_units(src, target, parents, units, "test source"))
    assert "already in the target, skipped" in capsys.readouterr().out
    assert len(list(target.ancestry(branch="main"))) == before


def test_check_catches_a_changed_chunk(source, tmp_path):
    repo, prefix, _ = source
    src, target = copy_all(repo, prefix, tmp_path)
    session = target.writable_session("main")
    session.store.set_virtual_ref("a/v/c/1", prefix + "data.bin", offset=4, length=4)
    commit(session, "tamper")
    dst = target.readonly_session("main").store
    with pytest.raises(AssertionError, match="reference differs"):
        run(check(src, dst, "a", sample=10))


def test_a_reference_outside_the_target_containers_is_refused(source, tmp_path):
    repo, _, _ = source
    src = open_source(repo)
    target = create_repo(tmp_path / "dst", [], prefixes=["file:///elsewhere/"])
    parents, units = run(units_at(src, 1))
    with pytest.raises(RuntimeError, match="outside the target's virtual chunk containers"):
        run(copy_units(src, target, parents, units, "test source"))
