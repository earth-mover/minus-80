import inspect
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import launch
from sources import SAMPLES
from tests.test_verify import _verify, full_store  # noqa: F401

TOKEN = "fake-token-0123456789"


STARTING = launch.Status("starting", None, None)
PENDING = launch.Status("starting", "pending", None)
RUNNING = launch.Status("ready", "running", None)


def done(code):
    return launch.Status("ready", "done", code)


class FakeVm:
    """The thing that starts and stops a VM, scripted: the launcher's own logic runs for real.

    ``states`` are the results of the successive ``poll`` calls and ``reads`` those of ``new_lines``; the last state
    repeats, a read list that runs out gives nothing, and an exception in either list is raised.
    A log read in a state with no running task raises ``error``, as the log service does.
    """

    def __init__(self, reads=(), states=None, code=0, start_error=None, error=None):
        self.reads, self.states = list(reads), list(states or [done(code)])
        self.start_error, self.error = start_error, error
        self.started, self.stopped, self.events, self.read_in = [], [], [], []
        self.status = launch.Status(None, None, None)
        self.cluster_id = 7

    def start(self, job, token):
        self.started.append((job, token))
        if self.start_error:
            raise self.start_error

    def poll(self):
        self.events.append("poll")
        item = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(item, Exception):
            raise item
        self.status = item
        return item

    def new_lines(self):
        self.events.append("read")
        self.read_in.append(self.status)
        if self.error and self.status.task not in ("assigned", "running", "done", "error"):
            raise self.error
        item = self.reads.pop(0) if self.reads else []
        if isinstance(item, Exception):
            raise item
        return item

    def stop(self, name):
        self.events.append("stop")
        self.stopped.append(name)


class Ticks:
    """A clock that advances ``step`` seconds each time it is read."""

    def __init__(self, step=60):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def _job(**changes):
    return launch.Job(**{"name": "atera-x", "vm_type": "m7g.xlarge", "region": "us-west-2",
                         "workspace": "earthmover-devs", "disk": 100, "minutes": 90, "script": "measure.py",
                         "arguments": ["--part", "plane"], **changes})


def _run(vm, job=None, step=60, **kwargs):
    lines = []
    code = launch.run(vm, job or _job(), TOKEN, out=lines.append, clock=Ticks(step), sleep=lambda s: None, **kwargs)
    return code, lines


def _remote(lines):
    """The lines of ``lines`` that are not the launcher's state lines."""
    return [line for line in lines if not line.startswith(("cluster:", "task:"))]


def _token_file(tmp_path, text=TOKEN + "\n"):
    path = tmp_path / "token"
    path.write_text(text)
    return path


def _argv(token_file, *extra, name="atera-measure"):
    return ["--vm-type", "m7g.xlarge", "--region", "us-west-2", "--workspace", "earthmover-devs", "--disk", "100",
            "--name", name, "--token-file", str(token_file), *extra, "--", "measure.py", "--part", "plane"]


def test_dry_run_prints_the_command_with_stars_and_never_the_token(tmp_path, capsys):
    assert launch.main(_argv(_token_file(tmp_path), "--dry-run")) == 0
    shown = capsys.readouterr()
    for text in (shown.out, shown.err):
        assert TOKEN not in text
    assert "'ARRAYLAKE_TOKEN': '***'" in shown.out
    for fact in ("m7g.xlarge", "us-west-2", "earthmover-devs", "BillingCategory", "measure.py --part plane",
                 "'cluster_timeout': '90 minutes'", "job_timeout='90 minutes'"):
        assert fact in shown.out


def test_dry_run_contacts_nothing(tmp_path, monkeypatch):
    def refuse():
        raise AssertionError("dry run built a client")

    monkeypatch.setattr(launch, "CoiledVm", refuse)
    assert launch.main(_argv(_token_file(tmp_path), "--dry-run")) == 0


def test_a_token_with_whitespace_inside_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="whitespace"):
        launch.read_token(_token_file(tmp_path, "abc def\n"))


@pytest.mark.parametrize("text", ["", "\n  \n"])
def test_a_missing_or_empty_token_file_is_an_error_before_any_launch(tmp_path, monkeypatch, text):
    monkeypatch.setattr(launch, "CoiledVm", lambda: pytest.fail("a client was built"))
    with pytest.raises(SystemExit, match="token"):
        launch.main(_argv(_token_file(tmp_path, text)))
    with pytest.raises(SystemExit, match="token"):
        launch.main(_argv(tmp_path / "absent", "--dry-run"))


def test_the_name_is_unique_for_each_launch():
    assert launch.unique_name("atera-measure") != launch.unique_name("atera-measure")
    assert launch.unique_name("atera-measure").startswith("atera-measure-")


def test_two_dry_runs_print_two_names(tmp_path, capsys):
    argv = _argv(_token_file(tmp_path), "--dry-run")
    launch.main(argv)
    first = capsys.readouterr().out
    launch.main(argv)
    assert first != capsys.readouterr().out


def test_every_forwarded_line_has_the_token_replaced(tmp_path):
    code, lines = _run(FakeVm([[f"token is {TOKEN}", "plain", f"{TOKEN} and {TOKEN}"]]), step=1)
    assert code == 0
    assert _remote(lines) == ["token is ***", "plain", "*** and ***"]


def test_the_exit_code_is_the_scripts_and_the_vm_stops_when_the_script_fails():
    vm = FakeVm([["boom"]], code=3)
    code, lines = _run(vm, step=1)
    assert (code, vm.stopped) == (3, ["atera-x"]) and "boom" in lines


def test_the_vm_stops_when_the_script_passes():
    vm = FakeVm(states=[RUNNING, RUNNING, done(0)])
    assert _run(vm)[0] == 0
    assert vm.stopped == ["atera-x"]


def test_the_vm_stops_and_the_exit_is_non_zero_when_the_time_limit_passes():
    vm = FakeVm(states=[RUNNING])
    code, lines = _run(vm, _job(minutes=5))
    assert code == launch.TIME_LIMIT_CODE != 0
    assert vm.stopped == ["atera-x"]
    assert any("5 minutes" in line for line in lines)


def test_the_time_limit_applies_while_the_cluster_starts_and_no_log_is_requested():
    vm = FakeVm(states=[STARTING])
    code, lines = _run(vm, _job(minutes=5))
    assert code == launch.TIME_LIMIT_CODE and vm.stopped == ["atera-x"] and vm.read_in == []


def test_the_vm_stops_when_the_launcher_is_interrupted():
    class Interrupted(FakeVm):
        def new_lines(self):
            raise KeyboardInterrupt

    vm = Interrupted(states=[RUNNING])
    with pytest.raises(KeyboardInterrupt):
        _run(vm)
    assert vm.stopped == ["atera-x"]


def test_the_vm_stops_when_the_start_fails_and_the_error_text_has_no_token():
    vm = FakeVm(start_error=RuntimeError(f"rejected {TOKEN}"))
    code, lines = _run(vm)
    assert code == 1 and vm.stopped == ["atera-x"]
    assert lines == ["RuntimeError: rejected ***"]


def test_the_token_goes_to_the_client_as_the_secret_and_not_into_the_job():
    vm = FakeVm()
    _run(vm)
    job, token = vm.started[0]
    assert token == TOKEN and TOKEN not in repr(job)


def test_the_remote_script_syncs_the_lockfile_then_runs_the_script_with_its_arguments():
    script = launch.remote_script(_job(arguments=["--note", "it's here"]))
    assert "uv sync --frozen --package atera-ingest" in script
    assert "ingests/atera/measure.py --note 'it'\"'\"'s here'" in script
    assert TOKEN not in script


def test_the_call_caps_the_cluster_and_the_job_on_the_server():
    kwargs = launch.run_kwargs(_job(minutes=45))
    assert kwargs["cluster_kwargs"] == {"cluster_timeout": "45 minutes"}
    assert kwargs["job_timeout"] == "45 minutes"
    assert "secret_env" not in kwargs and TOKEN not in repr(kwargs)


def test_the_call_text_shows_the_call_with_stars_for_the_secret():
    text = launch.call_text(_job())
    assert "'ARRAYLAKE_TOKEN': '***'" in text and "BillingCategory" in text and launch.IMAGE in text


def test_the_drain_reads_until_two_reads_in_a_row_are_empty_and_then_stops_the_vm():
    vm = FakeVm([["last"], [], ["late"], [], [], ["never"]], code=0)
    code, lines = _run(vm, step=1)
    assert (code, _remote(lines)) == (0, ["last", "late"])
    assert vm.events[-1] == "stop" and vm.events.count("read") == 5


def test_the_drain_ends_at_sixty_seconds_when_lines_never_stop():
    vm = FakeVm([["x"]] * 1000)
    code, lines = _run(vm, step=10)
    assert code == 0 and vm.stopped == ["atera-x"]
    assert 5 <= lines.count("x") <= 8


def test_a_stopped_cluster_without_an_exit_code_ends_the_run_with_one_after_the_drain():
    vm = FakeVm([["tail"]], states=[launch.Status("stopped", "assigned", None)])
    code, lines = _run(vm, step=1)
    assert code == 1 and "tail" in lines
    assert lines[-1] == "the cluster is stopped and the task has no exit code"
    assert vm.stopped == ["atera-x"]


def test_a_failed_log_read_in_the_drain_keeps_the_tasks_exit_code(coiled):
    vm = FakeVm([coiled.errors.ServerError("no log group")], code=3)
    code, lines = _run(vm, step=1)
    assert code == 3 and vm.stopped == ["atera-x"]
    assert [line for line in lines if line.startswith("logs:")] == ["logs: ServerError: no log group"]


def test_no_log_is_requested_before_a_task_runs_and_the_state_lines_show_each_change_once(coiled):
    error = coiled.errors.ServerError("Error pulling logs from CloudWatch: LogGroupName must be provided.")
    vm = FakeVm([["remote line"]], error=error,
                states=[STARTING, STARTING, PENDING, PENDING, launch.Status("ready", "pending", None), RUNNING,
                        RUNNING, done(3)])
    code, lines = _run(vm, step=1)
    assert code == 3 and "remote line" in lines
    assert not [line for line in lines if line.startswith("logs:")]
    assert all(status.task in ("running", "done") for status in vm.read_in)
    assert [line for line in lines if line.startswith(("cluster:", "task:"))] == [
        "cluster: starting", "task: pending", "cluster: ready", "task: running", "task: done"]


def test_a_failed_log_read_while_the_task_runs_is_one_short_line_and_the_next_poll_tries_again(coiled):
    error = coiled.errors.ServerError(f"slow {TOKEN}")
    vm = FakeVm([error, error, ["remote line"]], states=[RUNNING, RUNNING, RUNNING, done(0)])
    code, lines = _run(vm, step=1)
    assert code == 0 and "remote line" in lines
    assert [line for line in lines if line.startswith("logs:")] == ["logs: ServerError: slow ***"]


def test_log_reads_that_always_fail_never_end_a_run_whose_task_still_works(coiled):
    polls = 2 * launch.MAX_FAILURES
    vm = FakeVm([coiled.errors.ServerError("x")] * 1000, states=[*[RUNNING] * polls, done(0)])
    code, lines = _run(vm, step=1)
    assert code == 0 and vm.stopped == ["atera-x"]
    assert vm.events.count("read") >= polls
    assert not any("giving up" in line for line in lines)


def test_a_status_call_that_fails_once_is_tolerated(coiled):
    vm = FakeVm(states=[RUNNING, coiled.errors.ServerError("boom"), RUNNING, done(0)])
    code, lines = _run(vm, step=1)
    assert code == 0
    assert [line for line in lines if line.startswith("status:")] == ["status: ServerError: boom"]


def test_status_errors_while_the_cluster_starts_never_end_the_run(coiled):
    errors = [coiled.errors.ServerError("not yet")] * (3 * launch.MAX_FAILURES)
    vm = FakeVm(states=[*errors, RUNNING, done(0)])
    assert _run(vm, step=1)[0] == 0


def test_status_errors_in_a_row_while_the_task_runs_end_the_run_after_the_bound(coiled):
    vm = FakeVm(states=[RUNNING, *[coiled.errors.ServerError("x")] * (2 * launch.MAX_FAILURES)])
    code, lines = _run(vm, step=1)
    assert code != 0 and vm.stopped == ["atera-x"]


def test_the_stop_signals_raise_keyboard_interrupt_in_the_handler(monkeypatch):
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGHUP)}
    try:
        launch.install_stop_signals()
        for number in saved:
            with pytest.raises(KeyboardInterrupt):
                signal.getsignal(number)(number, None)
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP], ids=["sigterm", "sighup"])
def test_a_real_signal_to_the_launcher_process_stops_the_vm(tmp_path, number):
    script = tmp_path / "wait.py"
    script.write_text(f"""
import sys, time
sys.path.insert(0, {str(Path(launch.__file__).parent)!r})
import launch

class Vm:
    def start(self, job, token): print("started", flush=True)
    def new_lines(self): time.sleep(0.05); return []
    def poll(self): return launch.Status("ready", "running", None)
    def stop(self, name): print("stopped", flush=True)

launch.install_stop_signals()
job = launch.Job("n", "m7g.xlarge", "r", "w", 1, 90, "measure.py", [])
try:
    launch.run(Vm(), job, "tok-secret", interval=0.05)
except KeyboardInterrupt:
    print("interrupted", flush=True)
""")
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "started"
    process.send_signal(number)
    out, _ = process.communicate(timeout=30)
    assert out.split()[-2:] == ["stopped", "interrupted"]


@pytest.mark.parametrize("vm_type, arm", [("m7g.xlarge", True), ("c6gd.large", True), ("m7i.xlarge", False),
                                          ("r7iz.large", False), ("t4g.small", True)])
def test_a_graviton_type_is_an_arm_type(vm_type, arm):
    assert launch.is_arm(vm_type) is arm


@pytest.fixture
def coiled():
    """The installed client; the tests that use it need the ``launch`` extra."""
    pytest.importorskip("coiled")
    import coiled
    import coiled.errors
    import coiled.v2

    return coiled


def _vm():
    vm = launch.CoiledVm()
    vm.cluster_id, vm.job_id, vm.workspace = 7, 8, "earthmover-devs"
    return vm


def test_every_keyword_of_the_call_exists_in_the_clients_signature_and_the_secret_arrives(monkeypatch, tmp_path, coiled):
    parameters = set(inspect.signature(coiled.batch.run).parameters)
    cluster_parameters = set(inspect.signature(coiled.Cluster).parameters)
    calls = []
    monkeypatch.setattr(coiled.batch, "run", lambda *a, **k: calls.append((a, k)) or {"cluster_id": 7, "job_id": 8})
    monkeypatch.setattr(launch, "code_export", lambda: __import__("contextlib").nullcontext(tmp_path))
    vm = launch.CoiledVm()
    vm.start(_job(), TOKEN)

    (positional, kwargs), = calls
    assert positional == () and set(kwargs) <= parameters
    assert set(kwargs["cluster_kwargs"]) <= cluster_parameters
    assert kwargs["secret_env"] == {"ARRAYLAKE_TOKEN": TOKEN}
    assert TOKEN not in repr({k: v for k, v in kwargs.items() if k != "secret_env"})
    planned = launch.run_kwargs(_job())
    assert {k: v for k, v in kwargs.items() if k not in ("secret_env", "local_upload_path")} == {
        k: v for k, v in planned.items() if k != "local_upload_path"}
    assert (vm.cluster_id, vm.job_id) == (7, 8)


def _event(ts, message, instance=1):
    return {"timestamp": ts, "message": message, "instance_id": instance}


def test_log_requests_start_two_minutes_before_the_latest_event_and_a_late_event_is_kept(monkeypatch, coiled):
    t0 = 1_000_000_000_000
    replies = [[_event(t0 + 300_000, "__BATCH 8.0__ b")],
               [_event(t0 + 250_000, "a"), _event(t0 + 300_000, "__BATCH 8.0__ b")],
               [_event(t0 + 900_000, "c")]]
    since = []

    def fake(**kwargs):
        since.append(kwargs["since_ms"])
        return replies.pop(0)

    monkeypatch.setattr(coiled, "better_cluster_logs", fake)
    vm = _vm()
    assert [vm.new_lines(), vm.new_lines(), vm.new_lines()] == [["b"], ["a"], ["c"]]
    assert since == [None, t0 + 180_000, t0 + 180_000]
    # Events older than the next request's start cannot come back, so the set keeps only the window.
    assert len(vm.seen) == 1


def test_the_seen_set_stays_small_over_a_long_run(monkeypatch, coiled):
    t0 = 1_000_000_000_000
    clock = iter(range(10_000))
    monkeypatch.setattr(coiled, "better_cluster_logs",
                        lambda **kw: [_event(t0 + next(clock) * 5_000, f"line {t0}")])
    vm = _vm()
    for _ in range(5_000):
        vm.new_lines()
    assert len(vm.seen) <= 30


def _jobs(cluster, *tasks):
    return [{"id": 8, "cluster_state": cluster, "tasks": list(tasks)}]


@pytest.mark.parametrize("cluster, state, code, expected", [
    ("starting", "pending", None, launch.Status("starting", "pending", None)),
    ("ready", "assigned", None, launch.Status("ready", "assigned", None)),
    ("ready", "running", None, launch.Status("ready", "running", None)),
    ("ready", "running", 3, launch.Status("ready", "running", 3)),
    ("ready", "done", 0, launch.Status("ready", "done", 0)),
    ("ready", "done", None, launch.Status("ready", "done", 0)),
    ("ready", "error", None, launch.Status("ready", "error", 1))])
def test_the_status_comes_from_the_task(monkeypatch, cluster, state, code, expected, coiled):
    monkeypatch.setattr(coiled.batch, "status", lambda **k: _jobs(cluster, {"exit_code": code, "state": state}))
    assert _vm().poll() == expected


def test_a_job_without_tasks_or_without_jobs_has_no_task_state(monkeypatch, coiled):
    monkeypatch.setattr(coiled.batch, "status", lambda **k: _jobs("starting"))
    assert _vm().poll() == launch.Status("starting", None, None)
    monkeypatch.setattr(coiled.batch, "status", lambda **k: [])
    assert _vm().poll() == launch.Status(None, None, None)


def test_a_status_reply_that_is_not_a_list_is_an_error(monkeypatch, coiled):
    # The client prints the HTTP code and returns the error body, as a dict, when the service refuses.
    monkeypatch.setattr(coiled.batch, "status", lambda **k: {"message": "not ready"})
    with pytest.raises(launch.StatusError, match="not ready"):
        _vm().poll()


def _stop(coiled, monkeypatch, capsys, delete, states):
    deleted, reports = [], list(states)

    def fake_delete(**kwargs):
        deleted.append(kwargs)
        return delete(kwargs)

    def details(*a, **k):
        return {"current_state": {"state": reports.pop(0) if len(reports) > 1 else reports[0]}}

    monkeypatch.setattr(coiled, "delete_cluster", fake_delete)
    monkeypatch.setattr(coiled.v2, "cluster_details", details)
    vm = _vm()
    vm.clock, vm.sleep = Ticks(10), lambda seconds: None
    vm.stop("atera-x")
    return deleted, capsys.readouterr().out


def test_the_stop_goes_by_cluster_id_and_prints_the_name_and_the_state(monkeypatch, capsys, coiled):
    deleted, out = _stop(coiled, monkeypatch, capsys, lambda k: None, ["stopping"])
    assert deleted == [{"cluster_id": 7, "workspace": "earthmover-devs"}]
    assert out.strip() == "cluster atera-x: stopping"


def test_the_stop_waits_for_the_state_to_change_from_the_one_at_the_request(monkeypatch, capsys, coiled):
    _, out = _stop(coiled, monkeypatch, capsys, lambda k: None, ["starting", "starting", "stopping"])
    assert out.strip() == "cluster atera-x: stopping"


def test_a_cluster_that_is_neither_stopping_nor_stopped_after_sixty_seconds_gets_a_warning(
        monkeypatch, capsys, coiled):
    _, out = _stop(coiled, monkeypatch, capsys, lambda k: None, ["starting"])
    assert "cluster atera-x: starting" in out
    assert "warning" in out and "id 7" in out and "atera-x" in out and "stop it by hand" in out


def test_a_cluster_that_does_not_exist_is_a_stopped_cluster(monkeypatch, capsys, coiled):
    def gone(kwargs):
        raise coiled.errors.DoesNotExist

    _, out = _stop(coiled, monkeypatch, capsys, gone, ["stopped"])
    assert out.strip() == "cluster atera-x: stopped" and "by hand" not in out


def test_a_failed_stop_of_a_live_cluster_says_to_stop_it_by_hand(monkeypatch, capsys, coiled):
    def fails(kwargs):
        raise RuntimeError("denied")

    _, out = _stop(coiled, monkeypatch, capsys, fails, ["running"])
    assert "RuntimeError: denied" in out and "id 7" in out and "stop it by hand" in out


def test_a_failed_stop_of_a_cluster_that_is_stopped_is_not_an_error(monkeypatch, capsys, coiled):
    def fails(kwargs):
        raise RuntimeError("already stopped")

    _, out = _stop(coiled, monkeypatch, capsys, fails, ["stopped"])
    assert out.strip() == "cluster atera-x: stopped"


KIDNEY = SAMPLES["kidney"].name


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    """``main`` installs handlers for SIGTERM and SIGHUP; the test process gets its own back."""
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for number, handler in saved.items():
        signal.signal(number, handler)


def _ingest_argv(token_file, *extra, drop=()):
    argv = {"--vm-type": "m7g.xlarge", "--region": "us-west-2", "--workspace": "earthmover-devs",
            "--name": "atera-ingest", "--token-file": str(token_file), "--max-minutes": "600",
            "--sample": "kidney", "--repo": "arraylake://org/atera-kidney", "--catalog": "arraylake://org",
            "--namespace": "atera-kidney-tables", "--branch": "main", "--concurrency": "8"}
    flat = [x for k, v in argv.items() if k not in drop for x in (k, v)]
    return ["--ingest", *flat, *extra]


def test_the_ingest_dry_run_names_the_objects_the_local_files_and_the_full_mode_and_has_no_token(tmp_path, capsys):
    assert launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run")) == 0
    shown = capsys.readouterr()
    assert TOKEN not in shown.out + shown.err
    out = shown.out
    for fact in (f"{KIDNEY}_outs.zip", f"{KIDNEY}_he_image.ome.tif", f"{KIDNEY}_he_imagealignment.csv",
                 "45,720,445,144", "8c1c07343dbe1b946f5f90a21130782f-5451",
                 f'--bundle "$DATA/{KIDNEY}_outs.zip"', f'--he-image "$DATA/{KIDNEY}_he_image.ome.tif"',
                 f'--he-alignment "$DATA/{KIDNEY}_he_imagealignment.csv"', "fetch.py download", "fetch.py check",
                 "--head-source", "--namespace atera-kidney-tables", "--branch main", "--concurrency 8",
                 "'ARRAYLAKE_TOKEN': '***'", "'cluster_timeout': '600 minutes'", "job_timeout='600 minutes'", "disk_size='100'"):
        assert fact in out, fact
    assert out.count("--mode full") == 3
    assert "ingest.py" in out and "verify.py" in out


def test_elements_and_gates_reach_their_scripts(tmp_path, capsys):
    launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", "--elements", "labels/nucleus_labels",
                             "--gates", "complete,tables"))
    out = capsys.readouterr().out
    assert "--elements labels/nucleus_labels" in out and "--gates complete,tables" in out


def test_the_ingest_form_has_no_default_for_max_minutes(tmp_path):
    with pytest.raises(SystemExit, match="max-minutes"):
        launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", drop=("--max-minutes",)))


@pytest.mark.parametrize("missing", ["--sample", "--repo", "--catalog", "--namespace", "--branch", "--concurrency"])
def test_the_ingest_form_needs_each_of_its_arguments(tmp_path, missing):
    with pytest.raises(SystemExit, match=missing.removeprefix("--")):
        launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", drop=(missing,)))


def test_the_ingest_form_takes_no_script(tmp_path):
    with pytest.raises(SystemExit, match="script"):
        launch.main([*_ingest_argv(_token_file(tmp_path), "--dry-run"), "--", "measure.py"])


def test_the_disk_of_the_ingest_is_the_downloads_plus_the_overhead_and_a_smaller_disk_is_refused(tmp_path, capsys):
    # 48,518,883,775 bytes of downloads are 46 GiB, and the overhead is 40 GiB.
    assert launch.required_disk_gib("kidney") == 86
    assert launch.default_disk_gib("kidney") == 100
    launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", "--disk", "86"))
    assert "disk_size='86'" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="disk.*86"):
        launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", "--disk", "85"))


def test_the_help_says_that_a_dead_launcher_does_not_stop_the_ingest(capsys):
    with pytest.raises(SystemExit):
        launch.main(["--help"])
    assert "does not stop the job" in " ".join(capsys.readouterr().out.split())


def _ingest_job(**changes):
    plan = launch.IngestPlan("kidney", "arraylake://org/atera-kidney", "arraylake://org", "atera-kidney-tables", "main",
                             8, None, None, None)
    return _job(**{"script": "ingest", "arguments": [], "ingest": plan, "minutes": 600, "status_failures": None,
                   "stops": "viewer", **changes})


STUB = """#!/bin/bash
echo "uv $*" >> "$STUB_CALLS"
fail() { for i in $(seq 1 50); do echo "$1 line $i"; done; echo "the last line of $1"; exit "$2"; }
case "$*" in
  *"fetch.py download"*) [ "${STUB_FAIL:-}" = download ] && fail download 7 ;;
  *"fetch.py check"*) [ "${STUB_FAIL:-}" = check ] && fail check 1 ;;
  *"--preflight"*) [ "${STUB_FAIL:-}" = preflight ] && fail preflight 5 ;;
  *"ingest.py"*) [ "${STUB_FAIL:-}" = ingest ] && fail ingest 3 ;;
  *"verify.py"*) [ "${STUB_FAIL:-}" = verify ] && fail verify 4 ;;
esac
echo "stub ran: $*"
exit 0
"""


DF_STUB = """#!/bin/bash
echo "df $*" >> "$STUB_DF"
if [ "$1" = "-Pk" ]; then
  echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
  echo "stub 100 1 ${STUB_FREE_KB:-999999999999} 1% /"
else
  echo "stub df output for $*"
fi
"""


def _run_script(tmp_path, fail=None, job=None, free_kb=None):
    """Run the generated remote script for real with a stub ``uv`` that records each call."""
    work, bin_dir = tmp_path / "work", tmp_path / "bin"
    work.mkdir()
    bin_dir.mkdir()
    (work / "uv.lock").write_text("")
    (bin_dir / "uv").write_text(STUB)
    (bin_dir / "uv").chmod(0o755)
    (bin_dir / "df").write_text(DF_STUB)
    (bin_dir / "df").chmod(0o755)
    script = work / "remote.sh"
    script.write_text(launch.remote_script(job or _ingest_job()))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ATERA_DATA": str(tmp_path / "data"),
           "ATERA_LOGS": str(tmp_path / "logs"), "STUB_CALLS": str(tmp_path / "calls"), "STUB_DF": str(tmp_path / "df"),
           **({"STUB_FAIL": fail} if fail else {}), **({"STUB_FREE_KB": str(free_kb)} if free_kb else {})}
    done = subprocess.run(["bash", str(script)], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return done, calls


def test_the_script_runs_the_environment_the_download_the_check_the_ingest_and_the_verification_in_order(tmp_path):
    done, calls = _run_script(tmp_path)
    assert done.returncode == 0, done.stdout
    assert [c.split("python ")[-1].split(" --")[0] if "python" in c else c for c in calls] == [
        "uv sync --frozen --package atera-ingest", "ingests/atera/ingest.py", "ingests/atera/fetch.py download",
        "ingests/atera/fetch.py check", "ingests/atera/ingest.py", "ingests/atera/verify.py"]
    assert "--preflight" in calls[1] and "--bundle" not in calls[1] and "--mode full" in calls[1]
    assert f'--bundle {tmp_path}/data/{KIDNEY}_outs.zip' in calls[4] and "--mode full" in calls[4]
    assert "--preflight" not in calls[4] and "--mode full" in calls[5] and "--head-source" in calls[5]
    for step in ("environment", "preflight", "download", "check", "ingest", "verify"):
        assert f"=== step {step}" in done.stdout and f"=== step {step}: done in " in done.stdout


@pytest.mark.parametrize("fail, code, ran", [
    ("preflight", 5, 2), ("download", 7, 3), ("check", 1, 4), ("ingest", 3, 5), ("verify", 4, 6)])
def test_a_failed_step_stops_the_run_with_its_exit_code_and_the_later_steps_do_not_run(tmp_path, fail, code, ran):
    done, calls = _run_script(tmp_path, fail)
    assert done.returncode == code and len(calls) == ran
    assert f"=== step {fail}: FAILED with exit code {code}" in done.stdout


def test_a_failed_size_check_stops_before_the_ingest_and_the_last_forty_lines_print_again(tmp_path):
    done, calls = _run_script(tmp_path, "check")
    assert not any(("ingest.py" in c and "--preflight" not in c) or "verify.py" in c for c in calls)
    repeated = done.stdout.split("=== last 40 lines of step check\n")[1].splitlines()
    assert repeated[0] == "check line 12" and repeated[-1] == "the last line of check" and len(repeated) == 40


def test_the_script_stops_before_any_step_when_the_code_is_missing(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    done = subprocess.run(["bash", "-c", launch.remote_script(_ingest_job())], cwd=work, capture_output=True, text=True)
    assert done.returncode == 2 and "the uploaded code is missing" in done.stdout


def test_the_status_policy_of_the_script_form_ends_the_run_after_the_bound(coiled):
    errors = [coiled.errors.ServerError("down")] * 100
    vm = FakeVm(states=[RUNNING, *errors])
    code, lines = _run(vm, _job(status_failures=5), step=1)
    assert code != 0 and vm.stopped == ["atera-x"] and any("giving up" in line for line in lines)


def test_the_status_policy_of_the_ingest_form_keeps_polling_and_never_stops_the_vm_for_status_errors(coiled):
    errors = [coiled.errors.ServerError("no network")] * 500
    vm = FakeVm(states=[RUNNING, *errors, RUNNING, done(0)])
    code, lines = _run(vm, _ingest_job(), step=1)
    assert code == 0 and vm.stopped == ["atera-x"]
    status = [line for line in lines if line.startswith("status:")]
    assert 1 < len(status) <= 52 and "the job runs on" in status[-1]
    assert not any("giving up" in line for line in lines)


def test_a_job_with_no_launcher_limit_runs_until_the_task_ends():
    vm = FakeVm(states=[RUNNING] * 200 + [done(0)])
    assert _run(vm, _job(minutes=None), step=60)[0] == 0


def test_attach_needs_a_workspace(tmp_path):
    with pytest.raises(SystemExit, match="workspace"):
        launch.main(["--attach", "2364116", "--token-file", str(_token_file(tmp_path))])


def test_attach_forwards_the_log_with_the_token_replaced_and_ends_as_a_launch_ends(tmp_path, monkeypatch, capsys):
    vm = FakeVm([[f"progress {TOKEN}", "line two"], [], []], states=[RUNNING, done(0)])
    made = []
    monkeypatch.setattr(launch, "AttachedVm", lambda ref, tail: made.append((ref, tail)) or vm)
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    code = launch.main(["--attach", "atera-ingest-1", "--workspace", "earthmover-devs", "--tail", "7",
                        "--token-file", str(_token_file(tmp_path))])
    out = capsys.readouterr().out
    assert code == 0 and made == [("atera-ingest-1", 7)] and vm.stopped == []
    assert "progress ***" in out and "line two" in out and TOKEN not in out


def test_an_attached_vm_resolves_the_cluster_and_serves_the_last_lines_first(coiled, monkeypatch):
    asked = []
    jobs = [{"id": 8, "cluster_id": 7, "cluster_state": "ready", "tasks": [{"state": "running", "exit_code": None}]}]
    monkeypatch.setattr(coiled.batch, "status", lambda **k: asked.append(k) or jobs)
    events = [_event(1_000_000 + i, f"line {i}") for i in range(100)]
    replies = [events, [_event(1_000_200, "new line")]]
    monkeypatch.setattr(coiled, "better_cluster_logs", lambda **k: replies.pop(0))
    vm = launch.AttachedVm("2364116", 5)
    vm.start(launch.Job("2364116", "", "", "earthmover-devs", 0, None, "", []), TOKEN)
    assert asked == [{"cluster": 2364116, "workspace": "earthmover-devs"}]
    assert (vm.cluster_id, vm.job_id) == (7, 8)
    assert vm.new_lines() == [f"line {i}" for i in range(95, 100)] + ["new line"]


def test_verify_runs_the_source_gate_for_a_local_bundle_when_asked_to_head_the_objects(
        monkeypatch, capsys, full_store):  # noqa: F811
    import verify

    monkeypatch.setattr(verify, "gate_source", lambda root, sample, layouts: [])
    assert _verify(monkeypatch, full_store, "--mode", "full", "--gates", "source") == 0
    assert "source: skipped" in capsys.readouterr().out
    assert _verify(monkeypatch, full_store, "--mode", "full", "--gates", "source", "--head-source") == 0
    out = capsys.readouterr().out
    assert "source PASS" in out and "skipped" not in out


def _df(tmp_path):
    return (tmp_path / "df").read_text().splitlines() if (tmp_path / "df").exists() else []


def test_the_script_shows_the_disk_before_the_download_and_after_the_download_and_the_ingest(tmp_path):
    done, calls = _run_script(tmp_path)
    data = f"{tmp_path}/data"
    shown = [line for line in _df(tmp_path) if line.startswith("df -h")]
    assert shown[0].split()[2:5] == [data, "/opt", "."] and shown[1:] == [f"df -h {data}", f"df -h {data}"]
    out = done.stdout
    assert out.index("=== disk before the download") < out.index("=== step download: start")
    assert out.index("=== step download: done") < out.index("=== disk after the download") < out.index(
        "=== step ingest: start") < out.index("=== disk after the ingest") < out.index("=== step verify: start")


def test_the_script_stops_before_the_download_when_the_data_directory_has_too_little_space(tmp_path):
    need_kb = launch.download_bytes("kidney") // 1024 + launch.TILE_SCRATCH_GIB * (1 << 20)
    done, calls = _run_script(tmp_path, free_kb=need_kb - 1)
    assert done.returncode != 0
    assert not any("fetch.py" in c for c in calls)
    assert "not enough free space" in done.stdout and str(need_kb) in done.stdout
    (tmp_path / "again").mkdir()
    ok, calls = _run_script(tmp_path / "again", free_kb=need_kb)
    assert ok.returncode == 0


@pytest.mark.parametrize("flag, value, message", [
    ("--repo", "arraylake://org", "repo"),
    ("--repo", "s3://org/atera-kidney", "repo"),
    ("--repo", "arraylake://org/atera-kidney/x", "repo"),
    ("--catalog", "arraylake://org/atera-kidney", "catalog"),
    ("--catalog", "arraylake://other", "same org"),
    ("--namespace", "atera_kidney", "atera-kidney-tables"),
    ("--branch", "fixture", "branch"),
    ("--branch", "scratch", "branch"),
    ("--branch", "trial", "branch"),
    ("--sample", "brain", "atera-brain"),
    ("--sample", "kidny", "sample"),
    ("--concurrency", "0", "concurrency"),
    ("--max-minutes", "0", "max-minutes"),
    ("--max-minutes", "-5", "max-minutes")])
def test_the_ingest_form_refuses_a_wrong_argument_before_any_launch(tmp_path, monkeypatch, flag, value, message):
    monkeypatch.setattr(launch, "CoiledVm", lambda: pytest.fail("a client was built"))
    argv = _ingest_argv(_token_file(tmp_path))
    argv[argv.index(flag) + 1] = value
    with pytest.raises(SystemExit, match=message):
        launch.main(argv)


@pytest.mark.parametrize("flag, value", [("--elements", "a, b"), ("--elements", "a,,b"), ("--elements", ""),
                                         ("--gates", "tables,"), ("--gates", "x y"),
                                         ("--replace-tables", "a b"), ("--replace-tables", ",a")])
def test_a_name_list_with_an_empty_name_or_whitespace_is_refused(tmp_path, flag, value):
    with pytest.raises(SystemExit, match=flag.removeprefix("--")):
        launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", flag, value))


@pytest.mark.parametrize("branch", ["main", "trial-1", "trial-kidney-2"])
def test_the_ingest_form_accepts_main_and_trial_branches(tmp_path, capsys, branch):
    argv = _ingest_argv(_token_file(tmp_path), "--dry-run")
    argv[argv.index("--branch") + 1] = branch
    assert launch.main(argv) == 0


def test_the_script_form_refuses_a_time_limit_below_one_minute(tmp_path):
    with pytest.raises(SystemExit, match="max-minutes"):
        launch.main(_argv(_token_file(tmp_path), "--dry-run", "--max-minutes", "0"))


def test_replace_tables_goes_to_the_ingest_and_to_nothing_else(tmp_path, capsys):
    launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", "--replace-tables", "transcripts,nucleus_boundaries"))
    lines = capsys.readouterr().out.splitlines()
    with_flag = [line for line in lines if "--replace-tables transcripts,nucleus_boundaries" in line]
    assert len(with_flag) == 2 and all("ingest.py" in line for line in with_flag)
    with pytest.raises(SystemExit, match="replace-tables"):
        launch.main(_argv(_token_file(tmp_path), "--dry-run", "--replace-tables", "a"))


def _start_hint(vm_id=7, workspace="earthmover-devs"):
    return (f"follow: launch.py --attach {vm_id} --workspace {workspace}",
            f"stop: launch.py --stop {vm_id} --workspace {workspace}")


def test_an_attached_launcher_never_stops_the_cluster_on_any_path(coiled):
    never = {"stops": "never", "status_failures": None}
    boom = FakeVm(start_error=RuntimeError("status failed"))
    assert _run(boom, _job(**never))[0] == 1 and boom.stopped == []
    flaky = FakeVm(states=[RUNNING, coiled.errors.ServerError("x"), RUNNING, done(0)])
    assert _run(flaky, _job(**never), step=1)[0] == 0 and flaky.stopped == []
    broken = FakeVm(states=[None])
    assert _run(broken, _job(**never), step=1)[0] == 1 and broken.stopped == []
    slow = FakeVm(states=[RUNNING])
    assert _run(slow, _job(minutes=5, **never))[0] == launch.TIME_LIMIT_CODE and slow.stopped == []

    class Interrupted(FakeVm):
        def poll(self):
            raise KeyboardInterrupt

    interrupted = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        _run(interrupted, _job(**never))
    assert interrupted.stopped == []


def test_an_attached_launcher_says_how_to_stop_the_cluster_when_the_job_ends():
    vm = FakeVm(states=[done(0)])
    code, lines = _run(vm, _job(stops="never", name="2364116"), step=1)
    assert code == 0 and vm.stopped == []
    assert "stop: launch.py --stop 2364116 --workspace earthmover-devs" in " ".join(lines)


def test_the_ingest_launcher_prints_how_to_follow_and_stop_the_job_once_after_the_start():
    vm = FakeVm(states=[RUNNING, RUNNING, done(0)])
    code, lines = _run(vm, _ingest_job(), step=1)
    follow, stop = _start_hint()
    assert code == 0 and lines.count(follow) == 1 and lines.count(stop) == 1
    assert lines.index(follow) < lines.index("task: running")


def test_an_interrupt_of_the_ingest_launcher_leaves_the_job_and_prints_the_two_lines_again():
    class Interrupted(FakeVm):
        def poll(self):
            if len(self.events) > 3:
                raise KeyboardInterrupt
            return super().poll()

    vm, lines = Interrupted(states=[RUNNING]), []
    with pytest.raises(KeyboardInterrupt):
        launch.run(vm, _ingest_job(), TOKEN, out=lines.append, clock=Ticks(1), sleep=lambda s: None)
    assert vm.stopped == []
    assert all(line in lines for line in _start_hint()) and lines.count(_start_hint()[0]) == 2


def test_the_ingest_launcher_stops_the_vm_only_for_a_start_failure_the_time_limit_and_the_end_of_the_job():
    failed_start = FakeVm(start_error=RuntimeError("no cluster"))
    assert _run(failed_start, _ingest_job())[0] == 1 and failed_start.stopped == ["atera-x"]
    late = FakeVm(states=[RUNNING])
    assert _run(late, _ingest_job(minutes=5))[0] == launch.TIME_LIMIT_CODE and late.stopped == ["atera-x"]
    ended = FakeVm(states=[RUNNING, done(0)])
    assert _run(ended, _ingest_job(), step=1)[0] == 0 and ended.stopped == ["atera-x"]
    broken = FakeVm(states=[None])
    assert _run(broken, _ingest_job(), step=1)[0] == 1 and broken.stopped == []


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT], ids=["sigterm", "sighup", "sigint"])
def test_a_real_signal_does_not_stop_the_vm_of_an_ingest_launch(tmp_path, number):
    script = tmp_path / "wait.py"
    script.write_text(f"""
import signal, sys, time
sys.path.insert(0, {str(Path(launch.__file__).parent)!r})
import launch

class Vm:
    cluster_id = 7
    def start(self, job, token): print("started", flush=True)
    def poll(self): return launch.Status("ready", "running", None)
    def new_lines(self): time.sleep(0.05); return []
    def stop(self, name): print("STOPPED", flush=True)

launch.install_stop_signals()
# A child of a background job starts with SIGINT ignored; Python's own handler is what a terminal run has.
signal.signal(signal.SIGINT, signal.default_int_handler)
plan = launch.IngestPlan("kidney", "r", "c", "n", "main", 1, None, None, None)
job = launch.Job("n", "m7g.xlarge", "r", "ws", 1, 90, "ingest", [], plan, None, "viewer")
try:
    launch.run(Vm(), job, "tok-secret", out=lambda line: print(line, flush=True), interval=0.05)
except KeyboardInterrupt:
    print("interrupted", flush=True)
""")
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        if line.startswith("follow:"):
            break
    process.send_signal(number)
    out, _ = process.communicate(timeout=30)
    assert "STOPPED" not in out and "interrupted" in out and "stop: launch.py --stop 7 --workspace ws" in out


def test_stop_prints_the_name_and_the_state_deletes_the_cluster_and_needs_no_token(coiled, monkeypatch, capsys, tmp_path):
    states, deleted = ["running", "stopping"], []
    monkeypatch.setattr(coiled.v2, "cluster_details", lambda *a, **k: {"name": "atera-kidney-1",
                                                                       "current_state": {"state": states.pop(0)}})
    monkeypatch.setattr(coiled, "delete_cluster", lambda **k: deleted.append(k))
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    code = launch.main(["--stop", "2364116", "--workspace", "earthmover-devs", "--token-file", str(tmp_path / "none")])
    out = capsys.readouterr().out
    assert code == 0 and deleted == [{"cluster_id": 2364116, "workspace": "earthmover-devs"}]
    assert "atera-kidney-1" in out and "running" in out and out.strip().endswith("cluster atera-kidney-1: stopping")


def test_stop_by_name_finds_the_cluster_through_its_batch_job(coiled, monkeypatch, capsys):
    asked, deleted = [], []
    monkeypatch.setattr(coiled.batch, "status", lambda **k: asked.append(k) or [{"cluster_id": 9, "id": 3}])
    monkeypatch.setattr(coiled.v2, "cluster_details", lambda *a, **k: {"name": "n", "current_state": {"state": "stopped"}})
    monkeypatch.setattr(coiled, "delete_cluster", lambda **k: deleted.append(k))
    assert launch.main(["--stop", "atera-kidney-1", "--workspace", "w"]) == 0
    assert asked == [{"cluster": "atera-kidney-1", "workspace": "w"}] and deleted[0]["cluster_id"] == 9


def test_stop_needs_a_workspace():
    with pytest.raises(SystemExit, match="workspace"):
        launch.main(["--stop", "7"])


def test_the_help_and_the_note_state_who_stops_the_job(capsys):
    with pytest.raises(SystemExit):
        launch.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    for fact in ("--ingest", "never stops the VM", "except at its own --max-minutes limit", "--attach", "--stop"):
        assert fact in text, fact


def test_the_dry_run_has_a_preflight_step_after_the_environment_with_the_arguments_of_the_ingest(tmp_path, capsys):
    launch.main(_ingest_argv(_token_file(tmp_path), "--dry-run", "--elements", "labels/nucleus_labels",
                             "--replace-tables", "transcripts"))
    out = capsys.readouterr().out
    assert out.index("step environment") < out.index("step preflight") < out.index("=== disk before")
    assert out.index("step preflight") < out.index("step space") < out.index("step download")
    line = lambda step: next(x for x in out.splitlines() if x.startswith(f"step {step}"))  # noqa: E731
    preflight, ingest, verify = line("preflight"), line("ingest"), line("verify")
    assert "the arguments, the repo, the catalog and each table's state, with no write and no bundle" in preflight
    assert preflight.endswith("--preflight") and "--bundle" not in preflight and "--he-image" not in preflight
    for text in ("--replace-tables transcripts", "--elements labels/nucleus_labels", "--concurrency 8",
                 "--namespace atera-kidney-tables", "--mode full", "--branch main"):
        assert text in preflight and text in ingest, text
    assert "--replace-tables" not in verify


def test_a_failed_preflight_reaches_neither_the_space_step_nor_the_download(tmp_path):
    done, calls = _run_script(tmp_path, "preflight")
    assert done.returncode == 5 and len(calls) == 2
    assert "=== step space" not in done.stdout and not _df(tmp_path)
    assert "=== last 40 lines of step preflight" in done.stdout


def test_replace_tables_is_on_the_preflight_and_the_ingest_line_and_not_on_the_verify_line(tmp_path):
    plan = launch.IngestPlan("kidney", "arraylake://org/atera-kidney", "arraylake://org", "atera-kidney-tables", "main",
                             8, None, None, "transcripts,nucleus_boundaries")
    done, calls = _run_script(tmp_path, job=_ingest_job(ingest=plan))
    flagged = [c for c in calls if "--replace-tables transcripts,nucleus_boundaries" in c]
    assert [("--preflight" in c) for c in flagged] == [True, False] and "verify.py" not in " ".join(flagged)


def test_stop_with_dry_run_prints_what_it_would_delete_and_deletes_nothing(coiled, monkeypatch, capsys):
    monkeypatch.setattr(coiled.v2, "cluster_details", lambda *a, **k: {"name": "atera-kidney-1",
                                                                       "current_state": {"state": "running"}})
    monkeypatch.setattr(coiled, "delete_cluster", lambda **k: pytest.fail("a cluster was deleted"))
    assert launch.main(["--stop", "2364116", "--workspace", "earthmover-devs", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would stop" in out and "atera-kidney-1" in out and "running" in out


def test_attach_with_dry_run_prints_what_it_would_follow_and_does_nothing_else(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(launch, "AttachedVm", lambda *a: pytest.fail("an attach was made"))
    code = launch.main(["--attach", "2364116", "--workspace", "earthmover-devs", "--tail", "5", "--dry-run",
                        "--token-file", str(tmp_path / "none")])
    out = capsys.readouterr().out
    assert code == 0 and "would follow 2364116 in earthmover-devs" in out and "last 5 lines" in out


def test_an_interrupt_ends_the_ingest_launcher_with_130_and_the_hint_lines_and_no_traceback(tmp_path, monkeypatch,
                                                                                         capsys):
    class Interrupted(FakeVm):
        def poll(self):
            raise KeyboardInterrupt

    vm = Interrupted()
    monkeypatch.setattr(launch, "CoiledVm", lambda: vm)
    assert launch.main(_ingest_argv(_token_file(tmp_path))) == 130
    out = capsys.readouterr().out
    assert "follow: launch.py --attach 7" in out and "Traceback" not in out and vm.stopped == []


def test_an_interrupt_ends_an_attached_launcher_with_130(tmp_path, monkeypatch, capsys):
    class Interrupted(FakeVm):
        def poll(self):
            raise KeyboardInterrupt

    vm = Interrupted()
    monkeypatch.setattr(launch, "AttachedVm", lambda ref, tail: vm)
    assert launch.main(["--attach", "9", "--workspace", "w", "--token-file", str(_token_file(tmp_path))]) == 130
    assert vm.stopped == []


def test_the_docstring_says_which_forms_stop_the_vm_on_an_interrupt():
    doc = " ".join(launch.__doc__.split())
    assert "the script form stops the VM" in doc and "the ingest form and an attach do not" in doc


def _bare_vm(coiled, monkeypatch, statuses, details, delete):
    """A ``CoiledVm`` that knows no cluster id, over a fake client; the sleep and the clock cost nothing."""
    vm = launch.CoiledVm(clock=Ticks(10), sleep=lambda s: None)
    vm.workspace = "earthmover-devs"
    monkeypatch.setattr(coiled.batch, "status", statuses)
    monkeypatch.setattr(coiled.v2, "cluster_details", details)
    monkeypatch.setattr(coiled, "delete_cluster", delete)
    return vm


def test_a_stop_by_name_after_a_start_failure_prints_the_true_state_and_no_warning(coiled, monkeypatch, capsys):
    deleted = []
    vm = _bare_vm(coiled, monkeypatch, lambda **k: [{"cluster_id": 9, "id": 3}],
                  lambda *a, **k: {"name": "atera-x", "current_state": {"state": "stopping"}},
                  lambda **k: deleted.append(k))
    vm.stop("atera-x")
    out = capsys.readouterr().out
    assert out.strip() == "cluster atera-x: stopping" and deleted == [{"cluster_id": 9, "workspace": "earthmover-devs"}]


def test_a_stop_by_name_of_a_cluster_that_was_never_made_says_absent_and_does_not_warn(coiled, monkeypatch, capsys):
    def none(**k):
        raise RuntimeError("Unable to find cluster")

    def gone(**k):
        raise coiled.errors.DoesNotExist

    vm = _bare_vm(coiled, monkeypatch, none, lambda *a, **k: {}, gone)
    vm.stop("atera-x")
    out = capsys.readouterr().out
    assert out.strip() == "cluster atera-x: absent"


def test_a_failed_stop_by_name_prints_one_line_with_the_job_name_and_the_cli_commands(coiled, monkeypatch, capsys):
    def none(**k):
        raise RuntimeError("Unable to find cluster")

    def denied(**k):
        raise RuntimeError("denied")

    vm = _bare_vm(coiled, monkeypatch, none, lambda *a, **k: {}, denied)
    vm.stop("atera-x")
    lines = [x for x in capsys.readouterr().out.splitlines() if "stop it by hand" in x]
    assert len(lines) == 1
    assert "atera-x" in lines[0] and "coiled cluster list --workspace earthmover-devs" in lines[0]
    assert "coiled cluster stop atera-x --workspace earthmover-devs" in lines[0]
