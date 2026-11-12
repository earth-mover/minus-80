"""The trial-branch flow of a run, script by script as subprocesses, on a bundle with the layout facts of the brain.

The flow is the launcher's: bundle check, preflight, ingest to a trial branch, verification, promote, verification of main.
A second ingest stops by SIGINT inside the 3D stack and runs again. Its store must equal the store of the first ingest.
"""

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
import zarr

import verify
from minus80.core.icechunk_io import open_repository
from minus80.core.tables import local_catalog
from tests.test_verify import BRAIN, BrainStore

pytestmark = pytest.mark.skipif(os.environ.get("ATERA_DRY_RUN") != "1",
                                reason="the dry run takes minutes; set ATERA_DRY_RUN=1 to run it")

SCRIPTS = Path(__file__).parents[1]
BRANCH = "trial-dry"
#: The gates that a synthetic bundle serves. ``masks`` and ``instances`` sample 50 cells, and the bundle has 3.
SERVED = [g for g in verify.GATES["full"] if g not in ("masks", "instances")]
#: The line of the ingest after which the driver sends SIGINT: the start of a level of the stack after the first.
STOP_LINE = "images/morphology_3d/s1: reading"
#: Keys of a table pin that each catalog sets for itself.
OWN_IDS = ("snapshot_id", "table_uuid")


def _sigint_default():
    # A child of a background job starts with SIGINT ignored; then Python installs no KeyboardInterrupt handler.
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def run(cwd: Path, script: str, *args, stop_after: str | None = None) -> tuple[int, list[str]]:
    """Run ``script`` of the ingest with ``args``; print and return its exit code and its lines.

    With ``stop_after`` the driver sends SIGINT once after the first line that holds that text.
    """
    command = [sys.executable, str(SCRIPTS / script), *map(str, args)]
    print(f"\n$ {script} {' '.join(map(str, args))}", flush=True)
    lines = []
    with subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          preexec_fn=_sigint_default) as process:
        for line in process.stdout:
            lines.append(line.rstrip("\n"))
            print(f"  | {lines[-1]}", flush=True)
            if stop_after and stop_after in line:
                process.send_signal(signal.SIGINT)
                print("  | <driver: SIGINT>", flush=True)
                stop_after = None
    code = process.returncode
    print(f"  exit code {code}", flush=True)
    return code, lines


def _inputs(store: BrainStore) -> list:
    return ["--bundle", store.bundle, "--he-image", store.he, "--he-alignment", store.alignment, "--record", store.record]


def _ingest_args(store: BrainStore) -> list:
    """The arguments of the launcher's preflight and ingest steps, with a local repo and catalog."""
    return ["--sample", "brain", "--repo", store.repo, "--catalog", store.catalog, "--namespace", "ns",
            "--mode", "full", "--branch", BRANCH, "--concurrency", 2]


def _verify_args(store: BrainStore, branch: str) -> list:
    return ["--sample", "brain", "--repo", store.repo, "--catalog", store.catalog, "--mode", "full", "--branch", branch,
            *_inputs(store)]


def _tree(store: BrainStore) -> tuple[dict, dict]:
    """Each array of the trial branch with its metadata and bytes, and each node's attributes without own table ids."""
    with zarr.config.set({"array.rectilinear_chunks": True}):
        root = verify.open_root(str(store.repo), BRANCH)
        arrays, attrs = {}, {"": _without_ids(root.attrs.asdict())}
        for path, node in root.members(max_depth=None):
            attrs[path] = _without_ids(node.attrs.asdict())
            if isinstance(node, zarr.Array):
                arrays[path] = (node.shape, str(node.dtype), node.metadata.to_dict(), _values(node))
    return arrays, attrs


def _values(array):
    values = np.asarray(array[...])
    # The bytes of a string array are pointers, so its values compare as a list.
    return values.tolist() if values.dtype.kind in "OTU" else values.tobytes()


def _without_ids(value):
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k not in OWN_IDS}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


def _tables(store: BrainStore) -> dict:
    """The rows of each table in the catalog, at its current snapshot, sorted by their first column."""
    catalog = local_catalog(store.catalog)
    found = {}
    for namespace in catalog.list_namespaces():
        for identifier in catalog.list_tables(namespace):
            table = catalog.load_table(identifier).scan().to_arrow()
            found[identifier[-1]] = table.sort_by(table.column_names[0])
    return found


def _messages(store: BrainStore) -> list[str]:
    repo = open_repository(store.repo)
    return [info.message for info in repo.ancestry(branch=BRANCH)]


def _snapshot(lines: list[str], branch: str) -> str:
    return next(line.split()[-1] for line in lines if line.startswith(f"branch {branch}: snapshot "))


def test_the_trial_branch_flow_on_a_bundle_with_the_layout_of_the_brain():
    with tempfile.TemporaryDirectory(prefix="atera-dry-run-") as scratch:
        scratch = Path(scratch)
        store = BrainStore(scratch / "flow")
        store.directory.mkdir()
        store.write_inputs()

        code, lines = run(scratch, "check_bundle.py", "--sample", "brain", *_inputs(store))
        assert code == 0 and "bundle check: 0 differences" in lines

        # One stain's file name differs from the record; the check fails before any download.
        stain = "morphology_2d/ch0002_18s.ome.tif"
        assert stain in BRAIN["morphology_2d"]["files"]
        renamed = BrainStore(scratch / "renamed")
        renamed.directory.mkdir()
        renamed.write_inputs(lambda members: members.__setitem__("morphology_2d/ch0002_28s.ome.tif", members.pop(stain)))
        code, lines = run(scratch, "check_bundle.py", "--sample", "brain", "--bundle", renamed.bundle,
                          *_inputs(store)[2:])
        assert code == 1 and "bundle check: 0 differences" not in lines
        assert any(line.startswith(f"members.{stain}: record ") for line in lines)

        code, lines = run(scratch, "ingest.py", *_ingest_args(store), "--record", store.record, "--preflight")
        assert code == 0 and lines[-1] == "preflight: no problem"
        code, lines = run(scratch, "ingest.py", *_ingest_args(store), *_inputs(store))
        assert code == 0

        # The launcher runs every gate. The synthetic bundle cannot serve two of them.
        code, lines = run(scratch, "verify.py", *_verify_args(store, BRANCH))
        assert code == 1
        assert sorted(line.split()[0] for line in lines if line.endswith(" FAIL")) == ["instances", "masks"]

        verified = scratch / "verified-snapshot"
        code, lines = run(scratch, "verify.py", *_verify_args(store, BRANCH), "--gates", ",".join(SERVED),
                          "--snapshot-file", verified)
        assert code == 0 and "source: skipped" in " ".join(lines)
        assert all(f"{gate} PASS" in lines for gate in SERVED if gate != "source")
        snapshot = verified.read_text().strip()
        assert snapshot == _snapshot(lines, BRANCH)

        code, lines = run(scratch, "promote.py", "--repo", store.repo, "--from", BRANCH, f"--expect={snapshot}")
        assert code == 0 and lines[-1].startswith("main: ") and lines[-1].endswith(f" -> {snapshot}")

        code, lines = run(scratch, "verify.py", *_verify_args(store, "main"), "--gates", ",".join(SERVED))
        assert code == 0 and _snapshot(lines, "main") == snapshot

        # The same ingest on a fresh repo and catalog, stopped inside the stack, then run again.
        stopped = BrainStore(scratch / "stopped")
        stopped.directory.mkdir()
        stopped.bundle, stopped.he, stopped.alignment, stopped.record = store.bundle, store.he, store.alignment, store.record
        code, lines = run(scratch, "ingest.py", *_ingest_args(stopped), *_inputs(stopped), stop_after=STOP_LINE)
        assert code == -signal.SIGINT
        stack = [line.split(":")[0] for line in lines if line.startswith("images/morphology_3d/s") and ", commit " in line]
        assert "images/morphology_3d/s0" in stack and len(stack) < len(BRAIN["morphology_3d"]["files"][
            "morphology_3d/ch0000_dapi_3d.ome.tif"]["levels"])
        code, lines = run(scratch, "ingest.py", *_ingest_args(stopped), *_inputs(stopped))
        assert code == 0
        assert all(any(line.startswith(f"{level}: on branch {BRANCH} already") for line in lines) for level in stack)
        code, lines = run(scratch, "verify.py", *_verify_args(stopped, BRANCH), "--gates", ",".join(SERVED))
        assert code == 0

        (arrays, attrs), (arrays_stopped, attrs_stopped) = _tree(store), _tree(stopped)
        print(f"\ncompare: {len(attrs)} nodes, {len(arrays)} arrays")
        assert attrs_stopped == attrs
        assert arrays_stopped.keys() == arrays.keys()
        assert [p for p in arrays if arrays_stopped[p] != arrays[p]] == []
        tables, tables_stopped = _tables(store), _tables(stopped)
        print(f"compare: tables {', '.join(f'{n} {len(t):,} rows' for n, t in sorted(tables.items()))}")
        assert tables_stopped.keys() == tables.keys()
        assert all(tables_stopped[n].equals(tables[n]) for n in tables)
        messages, messages_stopped = _messages(store), _messages(stopped)
        print(f"compare: {len(messages)} commits unstopped, {len(messages_stopped)} commits stopped and run again")
        assert messages_stopped == messages
