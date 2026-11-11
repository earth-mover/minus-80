import inspect
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import launch

TOKEN = "fake-token-0123456789"


class FakeVm:
    """The thing that starts and stops a VM, scripted: the launcher's own logic runs for real.

    ``reads`` are the results of the successive ``new_lines`` calls; later calls return nothing.
    ``events`` records the order of the calls.
    """

    def __init__(self, reads=(), code=0, start_error=None, running_polls=0, ended=None):
        self.reads, self.code, self.start_error, self.running_polls = list(reads), code, start_error, running_polls
        self.ended, self.started, self.stopped, self.events = ended, [], [], []

    def start(self, job, token):
        self.started.append((job, token))
        if self.start_error:
            raise self.start_error

    def new_lines(self):
        self.events.append("read")
        return self.reads.pop(0) if self.reads else []

    def exit_code(self):
        if self.ended:
            raise self.ended
        if self.running_polls is None:
            return None
        if self.running_polls:
            self.running_polls -= 1
            return None
        return self.code

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
    code, lines = _run(FakeVm([[f"token is {TOKEN}", "plain", f"{TOKEN} and {TOKEN}"]]))
    assert code == 0
    assert lines == ["token is ***", "plain", "*** and ***"]


def test_the_exit_code_is_the_scripts_and_the_vm_stops_when_the_script_fails():
    vm = FakeVm([["boom"]], code=3)
    code, lines = _run(vm)
    assert (code, lines, vm.stopped) == (3, ["boom"], ["atera-x"])


def test_the_vm_stops_when_the_script_passes():
    vm = FakeVm(code=0, running_polls=2)
    assert _run(vm)[0] == 0
    assert vm.stopped == ["atera-x"]


def test_the_vm_stops_and_the_exit_is_non_zero_when_the_time_limit_passes():
    vm = FakeVm(running_polls=None)
    code, lines = _run(vm, _job(minutes=5))
    assert code == launch.TIME_LIMIT_CODE != 0
    assert vm.stopped == ["atera-x"]
    assert any("5 minutes" in line for line in lines)


def test_the_vm_stops_when_the_launcher_is_interrupted():
    class Interrupted(FakeVm):
        def new_lines(self):
            raise KeyboardInterrupt

    vm = Interrupted()
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
    vm = FakeVm([[], ["last"], [], ["late"], [], [], ["never"]], code=0)
    code, lines = _run(vm, step=1)
    assert (code, lines) == (0, ["last", "late"])
    assert vm.events[-1] == "stop" and vm.events.count("read") == 6


def test_the_drain_ends_at_sixty_seconds_when_lines_never_stop():
    vm = FakeVm([["x"]] * 1000)
    code, lines = _run(vm, step=10)
    assert code == 0 and vm.stopped == ["atera-x"]
    assert 5 <= len(lines) <= 8


def test_a_stopped_cluster_without_an_exit_code_ends_the_run_with_one_after_the_drain():
    vm = FakeVm([[], ["tail"]], ended=launch.ClusterStopped("the cluster is stopped and the task has no exit code"))
    code, lines = _run(vm, step=1)
    assert code == 1 and "tail" in lines
    assert lines[-1] == "the cluster is stopped and the task has no exit code"
    assert vm.stopped == ["atera-x"]


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
    def exit_code(self): return None
    def stop(self, name): print("stopped", flush=True)

launch.install_stop_signals()
job = launch.Job("n", "m7g.xlarge", "r", "w", 1, 90, "measure.py", [])
try:
    launch.run(Vm(), job, "t", interval=0.05)
except KeyboardInterrupt:
    print("interrupted", flush=True)
""")
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "started"
    process.send_signal(number)
    out, _ = process.communicate(timeout=30)
    assert out.split() == ["stopped", "interrupted"]


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


@pytest.mark.parametrize("state, code, expected", [
    ("running", None, None), ("stopped", 0, 0), ("running", 3, 3), ("done", None, 0)])
def test_the_exit_code_comes_from_the_task(monkeypatch, state, code, expected, coiled):
    task = {"exit_code": code, "state": state}
    monkeypatch.setattr(coiled.batch, "status", lambda **k: [{"cluster_state": "ready", "tasks": [task]}])
    assert _vm().exit_code() == expected


@pytest.mark.parametrize("cluster_state", ["stopped", "error"])
def test_a_dead_cluster_with_no_exit_code_raises(monkeypatch, cluster_state, coiled):
    task = {"exit_code": None, "state": "assigned"}
    monkeypatch.setattr(coiled.batch, "status", lambda **k: [{"cluster_state": cluster_state, "tasks": [task]}])
    with pytest.raises(launch.ClusterStopped, match=cluster_state):
        _vm().exit_code()


def _stop(coiled, monkeypatch, capsys, delete, state):
    deleted = []

    def fake_delete(**kwargs):
        deleted.append(kwargs)
        return delete(kwargs)

    monkeypatch.setattr(coiled, "delete_cluster", fake_delete)
    monkeypatch.setattr(coiled.v2, "cluster_details", lambda *a, **k: {"current_state": {"state": state}})
    _vm().stop("atera-x")
    return deleted, capsys.readouterr().out


def test_the_stop_goes_by_cluster_id_and_prints_the_name_and_the_state(monkeypatch, capsys, coiled):
    deleted, out = _stop(coiled, monkeypatch, capsys, lambda k: None, "stopping")
    assert deleted == [{"cluster_id": 7, "workspace": "earthmover-devs"}]
    assert out.strip() == "cluster atera-x: stopping"


def test_a_cluster_that_does_not_exist_is_a_stopped_cluster(monkeypatch, capsys, coiled):
    def gone(kwargs):
        raise coiled.errors.DoesNotExist

    _, out = _stop(coiled, monkeypatch, capsys, gone, "stopped")
    assert out.strip() == "cluster atera-x: stopped" and "by hand" not in out


def test_a_failed_stop_of_a_live_cluster_says_to_stop_it_by_hand(monkeypatch, capsys, coiled):
    def fails(kwargs):
        raise RuntimeError("denied")

    _, out = _stop(coiled, monkeypatch, capsys, fails, "running")
    assert "could not stop the cluster atera-x: RuntimeError: denied; stop it by hand" in out
    assert out.strip().endswith("cluster atera-x: running")
