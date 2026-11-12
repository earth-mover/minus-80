import icechunk
import pytest
import zarr

import promote
from minus80.core.icechunk_io import create_repo, open_repository


def _write(repo, branch, value):
    session = repo.writable_session(branch)
    root = zarr.group(session.store, overwrite=False)
    root.require_array("a", shape=(2,), dtype="i4", chunks=(2,))[:] = value
    return session.commit(f"value {value}")


@pytest.fixture
def repo(tmp_path):
    """A repo whose main holds one commit with value 0."""
    repo = create_repo(tmp_path / "repo", [])
    _write(repo, "main", 0)
    return repo


def _trial_ahead(repo, commits=3, name="trial-x"):
    repo.create_branch(name, repo.lookup_branch("main"))
    for value in range(1, commits + 1):
        last = _write(repo, name, value)
    return last


def _read(repo, branch):
    root = zarr.open_group(repo.readonly_session(branch).store, mode="r")
    return root["a"][:].tolist()


def _tips(repo):
    return {b: repo.lookup_branch(b) for b in repo.list_branches()}


def _unchanged(repo, before):
    assert _tips(repo) == before


def test_main_moves_to_the_trial_snapshot(repo):
    first = repo.lookup_branch("main")
    trial = _trial_ahead(repo)
    assert promote.promote(repo, "trial-x") == (first, trial)
    assert repo.lookup_branch("main") == trial
    assert "trial-x" in repo.list_branches()
    assert first in [s.id for s in repo.ancestry(branch="main")]
    assert _read(repo, "main") == [3, 3]


def test_a_commit_on_main_that_the_trial_lacks_is_refused(repo):
    _trial_ahead(repo)
    _write(repo, "main", 9)
    before = _tips(repo)
    with pytest.raises(ValueError, match="not an ancestor"):
        promote.promote(repo, "trial-x")
    _unchanged(repo, before)


def test_main_ahead_of_the_trial_is_refused(repo):
    repo.create_branch("trial-x", repo.lookup_branch("main"))
    _write(repo, "main", 9)
    before = _tips(repo)
    with pytest.raises(ValueError, match="not an ancestor"):
        promote.promote(repo, "trial-x")
    _unchanged(repo, before)


def test_equal_snapshots_change_nothing(repo):
    repo.create_branch("trial-x", repo.lookup_branch("main"))
    before = _tips(repo)
    old, new = promote.promote(repo, "trial-x")
    assert old == new == before["main"]
    _unchanged(repo, before)


@pytest.mark.parametrize(
    "source, target, message",
    [
        ("trial-absent", "main", "trial-absent"),
        ("other", "main", "trial-"),
        ("trial-x", "other", "main"),
    ],
)
def test_a_bad_source_or_target_is_refused(repo, source, target, message):
    _trial_ahead(repo)
    repo.create_branch("other", repo.lookup_branch("main"))
    before = _tips(repo)
    with pytest.raises(ValueError, match=message):
        promote.promote(repo, source, target)
    _unchanged(repo, before)


def test_a_dry_run_changes_nothing(repo):
    first = repo.lookup_branch("main")
    trial = _trial_ahead(repo)
    before = _tips(repo)
    assert promote.promote(repo, "trial-x", dry_run=True) == (first, trial)
    _unchanged(repo, before)


class _OtherWriterBeforeReset:
    """The real repo; a second writer commits to main just before the real reset."""

    def __init__(self, repo, other):
        self._repo, self._other = repo, other

    def __getattr__(self, name):
        return getattr(self._repo, name)

    def reset_branch(self, *args, **kwargs):
        self.moved = _write(self._other, "main", 9)
        return self._repo.reset_branch(*args, **kwargs)


def test_a_main_that_moved_after_the_read_is_not_overwritten(repo, tmp_path):
    _trial_ahead(repo)
    other = open_repository(tmp_path / "repo")
    racing = _OtherWriterBeforeReset(repo, other)
    with pytest.raises(icechunk.IcechunkError):
        promote.promote(racing, "trial-x")
    assert repo.lookup_branch("main") == racing.moved


def test_a_second_promote_finds_main_already_there(repo):
    _trial_ahead(repo)
    promote.promote(repo, "trial-x")
    old, new = promote.promote(repo, "trial-x")
    assert old == new


def _cli(monkeypatch, repo_dir, *args):
    monkeypatch.setattr("sys.argv", ["promote.py", "--repo", str(repo_dir), *args])
    with pytest.raises(SystemExit) as stop:
        promote.main()
    return stop.value.code


def test_the_command_prints_the_move(repo, tmp_path, monkeypatch, capsys):
    first = repo.lookup_branch("main")
    trial = _trial_ahead(repo)
    assert _cli(monkeypatch, tmp_path / "repo", "--from", "trial-x", "--dry-run") == 0
    assert capsys.readouterr().out == f"main: would move {first} -> {trial}\n"
    assert _cli(monkeypatch, tmp_path / "repo", "--from", "trial-x") == 0
    assert capsys.readouterr().out == f"main: {first} -> {trial}\n"
    assert _cli(monkeypatch, tmp_path / "repo", "--from", "trial-x") == 0
    assert capsys.readouterr().out == f"main is at {trial} already\n"


def test_a_refusal_is_one_line_on_stderr(repo, tmp_path, monkeypatch, capsys):
    assert _cli(monkeypatch, tmp_path / "repo", "--from", "trial-absent") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "Traceback" not in captured.err


def test_a_service_error_prints_its_type_only(repo, tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise icechunk.IcechunkError("s3://secret-bucket/prefix")

    monkeypatch.setattr(promote, "promote", fail)
    assert _cli(monkeypatch, tmp_path / "repo", "--from", "trial-x") == 1
    assert capsys.readouterr().err == "IcechunkError\n"


def test_an_error_of_the_opener_prints_its_type_only(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError("s3://secret")

    monkeypatch.setattr(promote, "open_repository", fail)
    assert _cli(monkeypatch, tmp_path / "repo", "--from", "trial-x") == 1
    captured = capsys.readouterr()
    assert "ValueError" in captured.err
    assert "secret" not in captured.out + captured.err


def test_a_missing_local_repo_is_not_created(tmp_path, monkeypatch, capsys):
    path = tmp_path / "absent"
    assert _cli(monkeypatch, path, "--from", "trial-x") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip().isidentifier()
    assert not path.exists()


def test_the_command_refuses_a_target_other_than_main(repo, tmp_path, monkeypatch, capsys):
    _trial_ahead(repo)
    before = _tips(repo)
    assert _cli(monkeypatch, tmp_path / "repo", "--from", "trial-x", "--to", "other") == 1
    assert "main" in capsys.readouterr().err
    _unchanged(repo, before)
