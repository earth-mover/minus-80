"""The branch that the full mode writes, on a local repo."""

import json
import sys

import pytest

import ingest
import promote
from minus80.core.icechunk_io import open_repository
from root_attrs import ROOT_KEYS
from tests.test_verify import Store


def test_a_full_run_on_a_trial_branch_leaves_main_at_the_first_commit_until_the_promote(monkeypatch, tmp_path):
    store = Store(tmp_path)
    store.write_inputs()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "ingest.py", "--sample", "kidney", "--repo", str(store.repo), "--catalog", str(store.catalog),
        "--namespace", "ns", "--mode", "full", "--branch", "trial-1", "--bundle", str(store.bundle),
        "--he-image", str(store.he), "--he-alignment", str(store.alignment), "--record", str(store.record),
        "--concurrency", "2", "--elements", "images/morphology"])
    ingest.main()

    repo = open_repository(store.repo)
    first = repo.lookup_branch("main")
    assert len(list(repo.ancestry(branch="main"))) == 1
    trial = [info.id for info in repo.ancestry(branch="trial-1")]
    assert len(trial) > 1 and trial[-1] == first

    assert promote.promote(repo, "trial-1") == (first, trial[0])
    assert repo.lookup_branch("main") == trial[0]


def _root_run(store, *flags):
    return ["ingest.py", "--sample", "kidney", "--repo", str(store.repo), "--catalog", str(store.catalog),
            "--namespace", "ns", "--mode", "full", "--branch", store.branch, "--bundle", str(store.bundle),
            "--he-image", str(store.he), "--he-alignment", str(store.alignment), "--record", str(store.record),
            "--elements", "root", *flags]


def _root_attrs(store) -> dict[str, str]:
    """Each root attribute of the branch as its JSON text."""
    return {k: json.dumps(v) for k, v in store.root().attrs.asdict().items()}


def test_root_keys_write_only_the_named_keys_and_refuse_an_unnamed_key_that_changes(monkeypatch, tmp_path, capsys):
    store = Store(tmp_path, "trial-1")
    store.write_inputs()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", _root_run(store))
    ingest.main()
    assert f"root attributes: changes {', '.join(ROOT_KEYS)}\n" in capsys.readouterr().out
    built = _root_attrs(store)

    store.edit(lambda root: root.attrs.update({"description": "earlier", "title": "other"}))
    edited, snapshot = _root_attrs(store), open_repository(store.repo).lookup_branch("trial-1")
    monkeypatch.setattr(sys, "argv", _root_run(store, "--root-keys", "description"))
    with pytest.raises(SystemExit, match=r"^root attributes: --root-keys does not name these keys that change: title$"):
        ingest.main()
    assert "root attributes: changes title, description\n" in capsys.readouterr().out
    assert open_repository(store.repo).lookup_branch("trial-1") == snapshot
    assert _root_attrs(store) == edited

    monkeypatch.setattr(sys, "argv", _root_run(store, "--root-keys", "description,title"))
    ingest.main()
    assert "root attributes: changes title, description\n" in capsys.readouterr().out
    assert _root_attrs(store) == built

    # A citation with its keys in another order is JSON-equal, so only a write of the key changes its bytes.
    citation = json.loads(built["citation"])
    store.edit(lambda root: root.attrs.update({"description": "earlier", "citation": dict(reversed(citation.items()))}))
    edited = _root_attrs(store)
    monkeypatch.setattr(sys, "argv", _root_run(store, "--root-keys", "description"))
    ingest.main()
    assert "root attributes: changes description\n" in capsys.readouterr().out
    assert _root_attrs(store) == {**edited, "description": built["description"]}
    assert _root_attrs(store)["citation"] != built["citation"]
    ingest.main()
    assert "root attributes: changes none\n" in capsys.readouterr().out


@pytest.mark.parametrize("flags, message", [
    (("--root-keys", "description,colour"), "cannot write the root keys colour; known: spatialdata_attrs, title"),
    (("--root-keys", "description", "--elements", "root,images/morphology"), "--root-keys needs --elements root alone"),
    (("--root-keys", "description", "--elements", "images/morphology"), "--root-keys needs --elements root alone"),
    (("--root-keys", "fixture", "--mode", "fixture", "--branch", "fixture"), "--root-keys is an option of --mode full")])
def test_root_keys_that_the_root_step_cannot_write_alone_are_refused_before_the_bundle_opens(
        monkeypatch, tmp_path, flags, message):
    monkeypatch.setattr(ingest, "open_bundle", lambda location: pytest.fail("the bundle opened"))
    store = Store(tmp_path, "trial-1")
    monkeypatch.setattr(sys, "argv", [*_root_run(store), *flags])
    with pytest.raises(SystemExit, match=message):
        ingest.main()
    assert list(tmp_path.iterdir()) == []


def test_a_preflight_checks_the_names_of_the_root_keys_and_writes_nothing(tmp_path):
    arguments = {"mode": "full", "branch": "trial-1", "repo": str(tmp_path / "repo"),
                 "catalog": str(tmp_path / "catalog"), "namespace": "ns", "elements": "root"}
    assert ingest.preflight(**arguments, root_keys="description,title") == []
    assert ingest.preflight(**arguments, root_keys="colour") == [
        f"cannot write the root keys colour; known: {', '.join(ROOT_KEYS)}"]
    assert ingest.preflight(**{**arguments, "elements": None}, root_keys="description") == [
        "--root-keys needs --elements root alone"]
    assert list(tmp_path.iterdir()) == []
