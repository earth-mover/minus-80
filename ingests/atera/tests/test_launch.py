import inspect
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import launch

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
