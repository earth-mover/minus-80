from types import SimpleNamespace
from uuid import uuid4

import pytest

import setup_sample
from setup_sample import Namespace, Org, SetupError, main, plan, policy_state, split_location

URL = "s3://fake-bucket-name/"
LOCATION = "s3://fake-bucket-name/fake-prefix-xyz/"
OTHER_LOCATION = "s3://fake-bucket-name/other-prefix/"
NICKNAME = "fake-nickname-bucket"
WRITES = ("create_repo", "create_namespace", "set_container", "set_policy")
REFERENCE_NAMESPACE = "atera-kidney-tables"


class FakeOrg:
    """The service, held in memory: ``calls`` records each call."""

    def __init__(self, *, repo=False, namespace=False, container=None, policy="absent", fail_on=None,
                 error=None, default=NICKNAME, reference=True, repo_bucket=NICKNAME, namespace_bucket=NICKNAME,
                 prefix="fake-prefix-xyz", lost=False):
        self.repo, self.namespace, self.held_container, self.held_policy = repo, namespace, container, policy
        self.fail_on, self.error, self.default, self.reference = fail_on, error, default, reference
        self.held_repo_bucket, self.namespace_bucket, self.prefix, self.lost = repo_bucket, namespace_bucket, prefix, lost
        self.calls = []

    def _call(self, name):
        self.calls.append(name)
        if name == self.fail_on:
            raise self.error or ConnectionError(f"cannot reach {LOCATION} now\nsecond line")

    def default_bucket(self):
        self._call("default_bucket")
        return self.default

    def repo_exists(self, name):
        self._call("repo_exists")
        return self.repo

    def repo_bucket(self, name):
        self._call("repo_bucket")
        return self.held_repo_bucket

    def create_repo(self, name):
        self._call("create_repo")
        self.repo = True

    def namespace_location(self, namespace):
        self._call("namespace_location")
        if namespace == REFERENCE_NAMESPACE:
            return Namespace(NICKNAME, "id-1", URL, "reference-prefix") if self.reference else None
        return Namespace(self.namespace_bucket, "id-1", URL, self.prefix) if self.namespace else None

    def create_namespace(self, namespace):
        self._call("create_namespace")
        self.namespace = not self.lost

    def container(self, repo):
        self._call("container")
        return self.held_container

    def set_container(self, repo, location, bucket):
        self._call("set_container")
        self.held_container = location

    def policy(self, bucket_id, subprefix):
        self._call("policy")
        return self.held_policy

    def set_policy(self, bucket, subprefix):
        self._call("set_policy")
        self.held_policy = "private"


def made(org):
    return [call for call in org.calls if call in WRITES]


def statuses(lines):
    """The status of each piece line after the bucket line."""
    return [line.split(": ", 1)[1] for line in lines[1:]]


def settled(**options):
    return FakeOrg(repo=True, namespace=True, container=LOCATION, policy="private", **options)


def test_fresh_org_creates_four_pieces_in_order():
    org = FakeOrg()
    lines, ok = plan("brain", org, dry_run=False)
    assert ok
    assert lines[0] == "bucket: as the reference sample"
    assert statuses(lines) == ["created"] * 4
    assert [line.split(": ")[0] for line in lines[1:]] == [
        "repo atera-brain", "namespace atera-brain-tables", "container", "policy"]
    assert made(org) == list(WRITES)


def test_rerun_skips_four_pieces():
    org = settled()
    lines, ok = plan("brain", org, dry_run=False)
    assert ok
    assert statuses(lines) == ["exists"] * 4
    assert made(org) == []


def test_container_for_another_location_stops_before_the_policy():
    org = FakeOrg(repo=True, namespace=True, container=OTHER_LOCATION)
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines[-1] == "container: DIFFERS (the repo has a container for another location)"
    assert len(lines) == 4
    assert "policy" not in org.calls and "set_container" not in org.calls


def test_container_without_the_trailing_slash_differs():
    org = FakeOrg(repo=True, namespace=True, container=LOCATION.rstrip("/"))
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok and lines[-1].startswith("container: DIFFERS")
    assert made(org) == []


def test_namespace_without_repo_creates_the_repo_only():
    org = FakeOrg(namespace=True)
    lines, ok = plan("brain", org, dry_run=False)
    assert ok
    assert statuses(lines)[:2] == ["created", "exists"]
    assert made(org) == ["create_repo", "set_container", "set_policy"]


def test_dry_run_on_a_fresh_org_creates_nothing():
    org = FakeOrg()
    lines, ok = plan("brain", org, dry_run=True)
    assert ok
    assert statuses(lines) == ["would create"] * 4
    assert made(org) == []


def test_dry_run_on_a_rerun_reports_exists():
    lines, _ = plan("brain", settled(), dry_run=True)
    assert statuses(lines) == ["exists"] * 4


def test_dry_run_with_a_repo_that_has_a_container_but_no_namespace_differs():
    org = FakeOrg(repo=True, container=OTHER_LOCATION)
    lines, ok = plan("brain", org, dry_run=True)
    assert not ok
    assert lines[-1] == "container: DIFFERS (the repo has a container for another location)"
    assert made(org) == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_no_line_holds_a_location(dry_run):
    for org in (FakeOrg(), FakeOrg(repo=True, namespace=True, container=OTHER_LOCATION)):
        text = "\n".join(plan("brain", org, dry_run=dry_run)[0])
        for secret in ("://", "fake-prefix-xyz", "other-prefix", "fake-bucket-name", NICKNAME):
            assert secret not in text


def test_service_error_in_the_second_piece_prints_only_its_type():
    org = FakeOrg(fail_on="create_namespace", error=KeyError(f"Bucket config '{NICKNAME}' not found"))
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines[-1] == "namespace atera-brain-tables: ERROR (KeyError)"
    assert NICKNAME not in "\n".join(lines)
    assert made(org) == ["create_repo", "create_namespace"]


def test_error_of_the_script_prints_its_text():
    org = FakeOrg(fail_on="container", repo=True, namespace=True, error=SetupError("the repo has 2 containers"))
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok and lines[-1] == "container: ERROR (SetupError: the repo has 2 containers)"


def test_namespace_that_does_not_show_after_creation_is_an_error():
    org = FakeOrg(lost=True)
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines[-1] == "namespace atera-brain-tables: ERROR (SetupError: the namespace does not show after its creation)"
    assert "would create" not in "\n".join(lines)
    assert made(org) == ["create_repo", "create_namespace"]


@pytest.mark.parametrize("prefix", ["", "/", "//"])
def test_namespace_without_a_sub_prefix_stops_before_the_container(prefix):
    org = FakeOrg(repo=True, namespace=True, prefix=prefix)
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines[-1] == "namespace atera-brain-tables: DIFFERS (the namespace has no sub-prefix of its own)"
    assert made(org) == [] and "container" not in org.calls and "policy" not in org.calls


@pytest.mark.parametrize("prefix", ["", "/", "//"])
def test_split_location_refuses_an_empty_prefix(prefix):
    with pytest.raises(ValueError):
        split_location(URL, prefix)


def test_split_location_gives_the_location_and_the_sub_prefix():
    assert split_location(URL, "a/b") == ("s3://fake-bucket-name/a/b/", "a/b")
    assert split_location("s3://fake-bucket-name", "/a/b/") == ("s3://fake-bucket-name/a/b/", "a/b")


@pytest.mark.parametrize(("policy", "text"), [
    ("public", "policy: DIFFERS (a public policy covers the location)"),
    ("wider", "policy: DIFFERS (a policy for a wider prefix covers the location)"),
])
def test_a_public_or_wider_policy_stops_without_a_write(policy, text):
    org = FakeOrg(repo=True, namespace=True, container=LOCATION, policy=policy)
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok and lines[-1] == text
    assert made(org) == []


def test_policy_state_exact_private():
    assert policy_state([("b", "a/b", False)], "b", "a/b") == "private"


def test_policy_state_exact_public():
    assert policy_state([("b", "a/b", True)], "b", "a/b") == "public"


def test_policy_state_exact_public_and_private_together_is_public():
    assert policy_state([("b", "a/b", False), ("b", "a/b", True)], "b", "a/b") == "public"


@pytest.mark.parametrize("public", [False, True])
def test_policy_state_ancestor_is_wider(public):
    assert policy_state([("b", "a", public)], "b", "a/b") == "wider"


def test_policy_state_bucket_root_is_wider():
    assert policy_state([("b", "", False)], "b", "a/b") == "wider"
    assert policy_state([("b", "/", True)], "b", "a/b") == "wider"


def test_policy_state_sibling_with_shared_leading_text_is_absent():
    assert policy_state([("b", "a/b_old", False)], "b", "a/b") == "absent"
    assert policy_state([("b", "a/b", False)], "b", "a/b_old") == "absent"


def test_policy_state_ignores_another_bucket_and_compares_without_slashes():
    assert policy_state([("other", "a/b", True), ("other", "", False)], "b", "a/b") == "absent"
    assert policy_state([("b", "/a/b/", False)], "b", "a/b") == "private"


def test_default_bucket_that_is_not_the_reference_bucket_stops_before_any_write():
    org = FakeOrg(default="another-nickname")
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines == ["bucket: DIFFERS (the default bucket is not the bucket of the reference sample)"]
    assert made(org) == []


def test_absent_reference_namespace_stops_before_any_write():
    org = FakeOrg(reference=False)
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok and lines[0].startswith("bucket: DIFFERS")
    assert made(org) == []


def test_repo_on_another_bucket_stops():
    org = FakeOrg(repo=True, repo_bucket="another-nickname")
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines == ["bucket: DIFFERS (the repo is not on the bucket of the reference sample)"]
    assert made(org) == []


def test_namespace_on_another_bucket_stops():
    org = FakeOrg(namespace=True, namespace_bucket="another-nickname")
    lines, ok = plan("brain", org, dry_run=False)
    assert not ok
    assert lines == ["bucket: DIFFERS (the namespace is not on the bucket of the reference sample)"]
    assert made(org) == []


def test_unknown_sample_is_refused(capsys):
    with pytest.raises(SystemExit):
        main(["--sample", "liver", "--org", "some-org"])
    assert "liver" in capsys.readouterr().err


def test_main_prints_the_lines_and_sets_the_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(setup_sample, "Org", lambda org: FakeOrg(repo=True, namespace=True, container=OTHER_LOCATION))
    assert main(["--sample", "kidney", "--org", "some-org"]) == 1
    assert "container: DIFFERS" in capsys.readouterr().out
    monkeypatch.setattr(setup_sample, "Org", lambda org: settled())
    assert main(["--sample", "brain", "--org", "some-org", "--dry-run"]) == 0


def test_main_dry_run_calls_reads_only(monkeypatch):
    org = FakeOrg()
    monkeypatch.setattr(setup_sample, "Org", lambda name: org)
    assert main(["--sample", "brain", "--org", "some-org", "--dry-run"]) == 0
    assert org.calls and not set(org.calls) & set(WRITES)


class FakeClient:
    """The Arraylake client's reads and writes used by ``Org``, with the client's return shapes."""

    def __init__(self, containers=None):
        self.outer, self.inner = uuid4(), uuid4()
        self.containers, self.writes = containers or {}, []

    def list_repos(self, org):
        return [SimpleNamespace(name="atera-brain", bucket=SimpleNamespace(nickname="nick-inner")),
                SimpleNamespace(name="atera-other", bucket=None)]

    def list_bucket_configs(self, org):
        return [SimpleNamespace(nickname="nick-outer", id=self.outer, url="s3://b/", is_default=False),
                SimpleNamespace(nickname="nick-inner", id=self.inner, url="s3://b/deep/", is_default=True)]

    def list_iceberg_namespaces(self, org):
        return [SimpleNamespace(name="atera-brain-tables", bucket_nickname="nick-outer", prefix="deep/x"),
                SimpleNamespace(name="linked", bucket_nickname=None, prefix="")]

    def get_virtual_chunk_containers(self, name):
        return self.containers

    def list_virtual_chunk_access_policies(self, org):
        return [SimpleNamespace(bucket_id=str(self.outer), subprefix="deep/x", public=False),
                SimpleNamespace(bucket_id=str(self.inner), subprefix="deep/x", public=True)]

    def update_virtual_chunk_containers(self, name, *, add=None, remove=None):
        self.writes.append(("update_virtual_chunk_containers", name, add))

    def set_virtual_chunk_access_policy(self, org, *, bucket_nickname, subprefix, public):
        self.writes.append(("set_virtual_chunk_access_policy", org, bucket_nickname, subprefix, public))

    def create_repo(self, name):
        self.writes.append(("create_repo", name))

    def create_iceberg_namespace(self, org, name):
        self.writes.append(("create_iceberg_namespace", org, name))


def test_org_reads_map_to_the_client_calls():
    client = FakeClient()
    org = Org("some-org", client=client)
    assert org.default_bucket() == "nick-inner"
    assert org.repo_exists("atera-brain") and not org.repo_exists("atera-none")
    assert org.repo_bucket("atera-brain") == "nick-inner"
    assert org.repo_bucket("atera-other") is None
    found = org.namespace_location("atera-brain-tables")
    assert found == Namespace("nick-outer", str(client.outer), "s3://b/", "deep/x")
    assert org.namespace_location("atera-absent") is None
    assert org.container("atera-brain") is None
    assert org.policy(str(client.outer), "deep/x") == "private"
    assert org.policy(str(client.inner), "deep/x") == "public"


def test_org_namespace_on_an_unlisted_bucket_is_an_error():
    client = FakeClient()
    client.list_bucket_configs = lambda org: []
    with pytest.raises(SetupError):
        Org("some-org", client=client).namespace_location("atera-brain-tables")
    with pytest.raises(SetupError):
        Org("some-org", client=client).default_bucket()


def test_org_with_several_containers_is_an_error():
    client = FakeClient({"s3://b/x/": "n", "s3://b/y/": "n"})
    with pytest.raises(SetupError):
        Org("some-org", client=client).container("atera-brain")


def test_org_writes_pass_the_namespaces_own_bucket_and_sub_prefix():
    client = FakeClient()
    org = Org("some-org", client=client)
    found = org.namespace_location("atera-brain-tables")
    location, subprefix = split_location(found.url, found.prefix)
    org.set_container("atera-brain", location, found.bucket)
    org.set_policy(found.bucket, subprefix)
    org.create_repo("atera-brain")
    org.create_namespace("atera-brain-tables")
    assert client.writes == [
        ("update_virtual_chunk_containers", "some-org/atera-brain", {"s3://b/deep/x/": "nick-outer"}),
        ("set_virtual_chunk_access_policy", "some-org", "nick-outer", "deep/x", False),
        ("create_repo", "some-org/atera-brain"),
        ("create_iceberg_namespace", "some-org", "atera-brain-tables"),
    ]
