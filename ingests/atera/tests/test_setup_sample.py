import pytest

import setup_sample
from setup_sample import main, plan

LOCATION = "s3://fake-bucket-name/fake-prefix-xyz/"
OTHER_LOCATION = "s3://fake-bucket-name/other-prefix/"
CREATES = ("create_repo", "create_namespace", "set_container", "set_policy")


class FakeOrg:
    """The service, held in memory: ``calls`` records each call, ``state`` what the org holds."""

    def __init__(self, *, repo=False, namespace=False, container=None, policy=False, fail_on=None):
        self.repo, self.namespace, self.held_container, self.held_policy = repo, namespace, container, policy
        self.fail_on, self.calls = fail_on, []

    def _call(self, name):
        self.calls.append(name)
        if name == self.fail_on:
            raise ConnectionError(f"cannot reach {LOCATION} now\nsecond line")

    def repo_exists(self, name):
        self._call("repo_exists")
        return self.repo

    def create_repo(self, name):
        self._call("create_repo")
        self.repo = True

    def namespace_location(self, namespace):
        self._call("namespace_location")
        return LOCATION if self.namespace else None

    def create_namespace(self, namespace):
        self._call("create_namespace")
        self.namespace = True

    def container(self, repo):
        self._call("container")
        return self.held_container

    def set_container(self, repo, location):
        self._call("set_container")
        self.held_container = location

    def policy(self, location):
        self._call("policy")
        return self.held_policy

    def set_policy(self, location):
        self._call("set_policy")
        self.held_policy = True


def made(org):
    return [call for call in org.calls if call in CREATES]


def test_fresh_org_creates_four_pieces_in_order():
    org = FakeOrg()
    lines, ok = plan("brain", org, dry_run=False)
    assert ok
    assert [line.split(": ")[1] for line in lines] == ["created"] * 4
    assert [line.split(": ")[0] for line in lines] == [
        "repo atera-brain", "namespace atera-brain-tables", "container", "policy"]
    assert made(org) == list(CREATES)


def test_rerun_skips_four_pieces():
    org = FakeOrg(repo=True, namespace=True, container=LOCATION, policy=True)
    lines, ok = plan("brain", org, dry_run=False)
    assert ok
    assert [line.split(": ")[1] for line in lines] == ["exists"] * 4
    assert made(org) == []


def test_container_for_another_location_stops_before_the_policy():
    org = FakeOrg(repo=True, namespace=True, container=OTHER_LOCATION)
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines[-1] == "container: DIFFERS (the repo has a container for another location)"
    assert len(lines) == 3
    assert "policy" not in org.calls and "set_container" not in org.calls


def test_namespace_without_repo_creates_the_repo_only():
    org = FakeOrg(namespace=True)
    lines, ok = plan("brain", org, dry_run=False)
    assert ok
    assert lines[0].endswith(": created") and lines[1].endswith(": exists")
    assert made(org) == ["create_repo", "set_container", "set_policy"]


def test_dry_run_on_a_fresh_org_creates_nothing():
    org = FakeOrg()
    lines, ok = plan("brain", org, dry_run=True)
    assert ok
    assert [line.split(": ")[1] for line in lines] == ["would create"] * 4
    assert made(org) == []


def test_dry_run_on_a_rerun_reports_exists():
    org = FakeOrg(repo=True, namespace=True, container=LOCATION, policy=True)
    lines, _ = plan("brain", org, dry_run=True)
    assert [line.split(": ")[1] for line in lines] == ["exists"] * 4


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_line_holds_a_location(dry_run):
    for org in (FakeOrg(), FakeOrg(repo=True, namespace=True, container=OTHER_LOCATION)):
        text = "\n".join(plan("brain", org, dry_run=dry_run)[0])
        for secret in ("://", "fake-prefix-xyz", "other-prefix", "fake-bucket-name"):
            assert secret not in text


def test_service_error_in_the_second_piece_stops_the_script():
    org = FakeOrg(fail_on="namespace_location")
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert len(lines) == 2 and lines[0].endswith(": created")
    assert lines[1].startswith("namespace atera-brain-tables: ERROR (ConnectionError: cannot reach")
    assert "://" not in lines[1] and "second line" not in lines[1]
    assert made(org) == ["create_repo"]


def test_unknown_sample_is_refused(capsys):
    with pytest.raises(SystemExit):
        main(["--sample", "liver", "--org", "some-org"])
    assert "liver" in capsys.readouterr().err


def test_main_prints_the_lines_and_sets_the_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(setup_sample, "Org", lambda org: FakeOrg(repo=True, namespace=True, container=OTHER_LOCATION))
    assert main(["--sample", "kidney", "--org", "some-org"]) == 1
    assert "container: DIFFERS" in capsys.readouterr().out
    monkeypatch.setattr(setup_sample, "Org", lambda org: FakeOrg(repo=True, namespace=True, container=LOCATION, policy=True))
    assert main(["--sample", "kidney", "--org", "some-org", "--dry-run"]) == 0
