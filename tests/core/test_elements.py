import numpy as np
import pytest
import zarr

from minus80.core.elements import COMPLETE, ElementWriter
from minus80.core.icechunk_io import create_repo
from minus80.core.remote import ReadCounter


def _repo(directory):
    repo = create_repo(directory, [])
    repo.create_branch("fixture", repo.lookup_branch("main"))
    return repo


@pytest.fixture
def repo(tmp_path):
    return _repo(tmp_path / "repo")


@pytest.fixture
def writer(repo):
    return ElementWriter(repo, "fixture")


def _write_level(root, level):
    root.require_group("images/m").create_array(f"s{level}", shape=(4,), dtype="u1", chunks=(2,))[:] = level


def _tree_bytes(repo, branch):
    """Every node of the branch with its attributes, and each array's shape, dtype and bytes."""
    root = zarr.open_group(repo.readonly_session(branch=branch).store, mode="r")
    tree = {"": dict(root.attrs)}
    for path, node in root.members(max_depth=None):
        if isinstance(node, zarr.Array):
            tree[path] = (dict(node.attrs), node.shape, str(node.dtype), node[:].tobytes())
        else:
            tree[path] = dict(node.attrs)
    return tree


def test_has_holds_only_the_nodes_a_commit_wrote(writer):
    assert not writer.has("labels/a")
    with writer.unit("labels/a", "write a") as (root, _):
        root.require_group("labels").create_group("a")
    assert writer.has("labels/a")
    assert not writer.has("labels/b")


def test_a_unit_that_raises_is_not_committed(repo, writer):
    snapshot = repo.lookup_branch("fixture")
    with pytest.raises(RuntimeError, match="read failed"), writer.unit("images/m/s0", "level 0") as (root, _):
        _write_level(root, 0)
        raise RuntimeError("read failed")
    assert repo.lookup_branch("fixture") == snapshot
    assert not writer.has("images/m/s0")


def test_a_unit_written_again_after_a_failed_one_commits(writer):
    with pytest.raises(RuntimeError), writer.unit("images/m/s0", "level 0") as (root, _):
        _write_level(root, 0)
        raise RuntimeError
    with writer.unit("images/m/s0", "level 0") as (root, _):
        _write_level(root, 0)
    assert writer.has("images/m/s0")


def test_a_unit_commits_with_its_message(repo, writer):
    with writer.unit("images/m/s0", "level 0") as (root, _):
        _write_level(root, 0)
    ancestry = list(repo.ancestry(branch="fixture"))
    assert ancestry[0].message == "level 0"


def test_a_unit_gives_a_progress_that_counts(writer, capsys):
    with writer.unit("images/m/s0", "level 0", total=8) as (root, progress):
        _write_level(root, 0)
        progress.advance(8)
    assert progress.done == 8
    assert "images/m/s0" in capsys.readouterr().out


def test_a_unit_reports_the_bytes_the_counter_saw(repo, capsys):
    counter = ReadCounter()
    writer = ElementWriter(repo, "fixture", counter=counter)
    with writer.unit("images/m/s0", "level 0") as (root, _):
        counter.bytes += 3 << 20
        _write_level(root, 0)
    assert "3.0 MiB read" in capsys.readouterr().out
    assert counter.progress is None


def test_an_element_is_complete_only_after_finish(writer):
    assert not writer.complete("images/m")
    with writer.unit("images/m/s0", "level 0") as (root, _):
        _write_level(root, 0)
    assert writer.has("images/m/s0") and not writer.complete("images/m")
    writer.finish("images/m", "images/m complete")
    assert writer.complete("images/m")
    assert zarr.open_group(writer.repo.readonly_session(branch="fixture").store, mode="r")["images/m"].attrs[COMPLETE] is True


def _write_element(writer, stop_after=None):
    """Write levels 0 and 1 of one element, skipping a level the branch holds, then mark it complete."""
    for level in (0, 1):
        if not writer.has(f"images/m/s{level}"):
            with writer.unit(f"images/m/s{level}", f"level {level}") as (root, _):
                _write_level(root, level)
        if stop_after == level:
            raise KeyboardInterrupt
    writer.finish("images/m", "images/m complete")


def test_a_stopped_run_resumes_to_the_same_store(repo, tmp_path):
    other = _repo(tmp_path / "other")
    writer = ElementWriter(repo, "fixture")
    with pytest.raises(KeyboardInterrupt):
        _write_element(writer, stop_after=0)
    assert writer.has("images/m/s0") and not writer.has("images/m/s1")
    _write_element(writer)
    _write_element(ElementWriter(other, "fixture"))
    assert _tree_bytes(repo, "fixture") == _tree_bytes(other, "fixture")
    assert _tree_bytes(repo, "fixture")["images/m/s1"][3] == np.full(4, 1, "u1").tobytes()


def test_a_protected_branch_is_refused(repo):
    with pytest.raises(ValueError, match="fixture"):
        ElementWriter(repo, "fixture", protected=("fixture",))
    ElementWriter(repo, "fixture", protected=("main",))


def test_has_attribute_holds_only_the_root_attributes_a_commit_wrote(repo, writer):
    assert not writer.has_attribute("k")
    with writer.unit("k", "Write k") as (root, _):
        root.attrs["k"] = {"a": 1}
    assert writer.has_attribute("k")
    assert not writer.has_attribute("other")
    with pytest.raises(RuntimeError):
        with writer.unit("other", "Write other") as (root, _):
            root.attrs["other"] = 1
            raise RuntimeError
    assert not writer.has_attribute("other")


def test_a_unit_that_changes_nothing_makes_no_commit_and_prints_one_line(repo, writer, capsys):
    before = [s.id for s in repo.ancestry(branch="fixture")]
    with writer.unit("root attributes", "Write root attributes") as (root, _):
        root.attrs["a"] = 1
    capsys.readouterr()
    held = [s.id for s in repo.ancestry(branch="fixture")]
    with writer.unit("root attributes", "Write root attributes") as (root, _):
        root.attrs["a"] = 1
    assert [s.id for s in repo.ancestry(branch="fixture")] == held and len(held) == len(before) + 1
    out = capsys.readouterr().out
    assert "root attributes: on branch fixture already, equal, skipped\n" in out and "commit" not in out
