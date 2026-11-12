"""The branch that the full mode writes, on a local repo."""

import sys

import ingest
import promote
from minus80.core.icechunk_io import open_repository
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
