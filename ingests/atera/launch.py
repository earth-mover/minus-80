"""Run one script of this ingest on one Coiled VM, from a clean copy of the committed code and the lockfile.

    uv run --package atera-ingest --extra launch python ingests/atera/launch.py --vm-type m7g.xlarge \\
        --region us-west-2 --workspace earthmover-devs --disk 100 --name atera-measure -- measure.py --part all ...

The VM builds its own environment from ``uv.lock`` inside a Linux container, never from the laptop's environment.
The launcher stops the VM when the script ends, fails or passes the time limit, or when the launcher is interrupted.
It exits with the script's exit code. The Arraylake token never appears in any output.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

HERE = Path(__file__).parent
#: The container that builds the environment: uv and git on Debian, for x86 and ARM.
IMAGE = "ghcr.io/astral-sh/uv:python3.12-bookworm"
TOKEN_VARIABLE = "ARRAYLAKE_TOKEN"
TAGS = {"BillingCategory": "RnD"}
#: The exit code that means the launcher ended the run at the time limit; the shell's code for a timeout.
TIME_LIMIT_CODE = 124
#: Placeholder for the directory that the call uploads; the directory exists only during the call.
UPLOAD = "<git archive of HEAD>"
#: The log service delivers late, and an event can arrive with a timestamp older than the newest one.
LOG_WINDOW_MS = 120_000
#: The longest the launcher reads the log after the task ends, and the empty reads in a row that end it earlier.
DRAIN_SECONDS, DRAIN_EMPTY_READS = 60, 2
#: Consecutive failed status or log calls, while the task runs, that end the run. 30 polls of 10 s are five minutes,
#: longer than the log service lags behind a new VM; before the task runs no failure counts.
MAX_FAILURES = 30
#: Task states of a batch job, as ``coiled batch wait`` counts them: ``assigned`` and ``running`` both mean the VM works.
RUNNING_TASK = ("assigned", "running")
#: The longest the launcher waits for the cluster to report ``stopping`` or ``stopped`` after the stop request.
STOP_SECONDS = 60
#: Coiled prefixes each line of a batch task's output with this marker.
BATCH_MARKER = re.compile(r"^__BATCH \d+\.\d+__ ?")


class StatusError(Exception):
    """The service refused a status request; the client returns the error body instead of raising."""


class Status(NamedTuple):
    """The cluster's state, the task's state and the task's exit code; each is ``None`` until it is known."""

    cluster: str | None
    task: str | None
    code: int | None


@dataclass(frozen=True)
class Job:
    """One launch. The token is not part of it."""

    name: str
    vm_type: str
    region: str
    workspace: str
    disk: int
    minutes: int
    script: str
    arguments: list[str]


def unique_name(prefix: str) -> str:
    """``prefix`` and a UTC timestamp and a random suffix; Coiled reattaches to a live cluster of the same name."""
    return f"{prefix}-{datetime.now(timezone.utc):%Y%m%dt%H%M%S}-{secrets.token_hex(2)}"


def read_token(path: Path) -> str:
    """The token in ``path``, without surrounding whitespace; a missing or empty file exits."""
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if not token:
        raise SystemExit(f"the token file {path} is missing or empty")
    if re.search(r"\s", token):
        raise SystemExit(f"the token in {path} holds whitespace; the file must hold the token only")
    return token


def is_arm(vm_type: str) -> bool:
    """True for an AWS Graviton type, whose family names carry a ``g`` after the generation: m7g, c6gd, t4g."""
    return re.match(r"^[a-z]+\d+[a-z]*g[a-z]*\.", vm_type) is not None


def remote_script(job: Job) -> str:
    """The shell script that the VM runs: build the environment from the lockfile, then run the ingest script."""
    run = shlex.join(["uv", "run", "--frozen", "--package", "atera-ingest", "python",
                      f"ingests/atera/{job.script}", *job.arguments])
    return f"""#!/bin/bash
set -euo pipefail
test -f uv.lock || {{ echo "the uploaded code is missing from $PWD"; exit 2; }}
# The environment lives outside the working directory, which Coiled syncs.
export UV_PROJECT_ENVIRONMENT=/opt/venv UV_CACHE_DIR=/tmp/uv-cache
echo "machine: $(uname -m)"
uv sync --frozen --package atera-ingest
exec {run}
"""


def run_kwargs(job: Job) -> dict:
    """The keywords of the ``coiled.batch.run`` call, without the secret; the dry run prints the same ones."""
    return {
        "command": remote_script(job), "command_as_script": True, "name": job.name, "workspace": job.workspace,
        "container": IMAGE, "ignore_container_entrypoint": True, "tag": TAGS, "vm_type": [job.vm_type],
        "arm": is_arm(job.vm_type), "region": job.region, "disk_size": str(job.disk), "local_upload_path": UPLOAD,
        "job_timeout": f"{job.minutes} minutes",
        # The server stops the cluster at this age even when the launcher is gone.
        "cluster_kwargs": {"cluster_timeout": f"{job.minutes} minutes"},
    }


def call_text(job: Job) -> str:
    """The call as text, with ``***`` for the secret and the script left out."""
    shown = run_kwargs(job) | {"command": "<remote script, below>", "secret_env": {TOKEN_VARIABLE: "***"}}
    return "\n".join(["coiled.batch.run(", *(f"    {key}={value!r}," for key, value in shown.items()), ")"])


def install_stop_signals() -> None:
    """Make SIGTERM and SIGHUP end the run as Ctrl-C does, so the ``finally`` that stops the VM runs."""

    def stop(number, frame):
        raise KeyboardInterrupt

    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, stop)


@contextlib.contextmanager
def code_export():
    """A temporary directory that holds the committed files of this repository, and nothing else."""
    root = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"], check=True,
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
                           check=True, capture_output=True, text=True).stdout
    if dirty:
        print("warning: uncommitted changes are not sent; the VM runs HEAD", flush=True)
    archive = subprocess.run(["git", "-C", root, "archive", "--format=tar", "HEAD"], check=True,
                             capture_output=True).stdout
    with tempfile.TemporaryDirectory() as directory, tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(directory, filter="data")
        yield Path(directory)


class CoiledVm:
    """One Coiled batch job on one VM: the task runs on the scheduler's VM, so no second VM starts."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.workspace = self.cluster_id = self.job_id = self.latest = None
        self.seen: set = set()

    def start(self, job: Job, token: str) -> None:
        import coiled

        self.workspace = job.workspace
        # Coiled scans the script for local files to upload; from the export they are the committed ones.
        with code_export() as directory, contextlib.chdir(directory):
            found = coiled.batch.run(**run_kwargs(job) | {"local_upload_path": str(directory),
                                                          "secret_env": {TOKEN_VARIABLE: token}})
        self.cluster_id, self.job_id = found["cluster_id"], found["job_id"]

    def new_lines(self) -> list[str]:
        """The task's output lines that earlier calls did not return."""
        import coiled

        since = None if self.latest is None else self.latest - LOG_WINDOW_MS
        events = coiled.better_cluster_logs(
            cluster_id=self.cluster_id, workspace=self.workspace, dask=True, system=False,
            since_ms=since, filter=f"__BATCH {self.job_id}.")
        lines = []
        for event in events:
            key = (event["timestamp"], event.get("instance_id"), event["message"])
            if key in self.seen:
                continue
            self.seen.add(key)
            self.latest = max(self.latest or 0, event["timestamp"])
            lines += [BATCH_MARKER.sub("", line) for line in event["message"].splitlines()]
        if self.latest is not None:
            # The next request starts here, so an older key cannot return and need not stay.
            floor = self.latest - LOG_WINDOW_MS
            self.seen = {key for key in self.seen if key[0] >= floor}
        return lines

    def poll(self) -> Status:
        """The cluster state, the task state and the task's exit code."""
        import coiled

        jobs = coiled.batch.status(cluster=self.cluster_id, workspace=self.workspace)
        if not isinstance(jobs, list):
            raise StatusError(f"unexpected status reply: {str(jobs)[:200]}")
        if not jobs:
            return Status(None, None, None)
        task = jobs[0]["tasks"][0] if jobs[0]["tasks"] else {"exit_code": None, "state": None}
        code = task["exit_code"]
        if code is None:
            code = {"error": 1, "done": 0}.get(task["state"])
        return Status(jobs[0].get("cluster_state"), task["state"], code)

    def state(self) -> str:
        import coiled.v2

        try:
            return coiled.v2.cluster_details(self.cluster_id, workspace=self.workspace)["current_state"]["state"]
        except Exception as error:
            return f"unknown ({type(error).__name__})"

    def stop(self, name: str) -> None:
        """Stop the cluster by id, or by name when the start failed; print the name and the state after."""
        import coiled
        from coiled.errors import DoesNotExist

        failure, gone = None, False
        try:
            target = {"cluster_id": self.cluster_id} if self.cluster_id else {"name": name}
            coiled.delete_cluster(**target, workspace=self.workspace)
        except DoesNotExist:
            gone = True
        except Exception as error:
            failure = error
        # The state read right after the request is the old one, so wait for it to change.
        state = "stopped" if gone else self.state_after_stop()
        print(f"cluster {name}: {state}", flush=True)
        if state not in ("stopping", "stopped"):
            cause = f" ({type(failure).__name__}: {failure})" if failure is not None else ""
            print(f"warning: cluster {name} (id {self.cluster_id}) is {state} {STOP_SECONDS} s after the stop "
                  f"request{cause}; stop it by hand", flush=True)

    def state_after_stop(self) -> str:
        if not self.cluster_id:
            return "unknown (no cluster id)"
        began = self.clock()
        while True:
            state = self.state()
            if state in ("stopping", "stopped") or self.clock() - began >= STOP_SECONDS:
                return state
            self.sleep(2)


def run(vm, job: Job, token: str, *, out: Callable[[str], object] = print, clock=time.monotonic, sleep=time.sleep,
        interval: float = 10) -> int:
    """Start ``job`` on ``vm``, forward its lines, and return its exit code; the VM stops whatever happens.

    Nothing asks for the log before a task runs. A failed call is one line and the next poll tries again.
    """

    def say(text):
        out(text.replace(token, "***"))

    def failed(what, error, streak):
        if streak == 1 or streak % 10 == 0:
            say(f"{what}: {type(error).__name__}: {error}")

    def drain():
        # The log lags the task, so read until it stays empty before the VM, and its log, go.
        began, empty, streak = clock(), 0, 0
        while empty < DRAIN_EMPTY_READS and clock() - began < DRAIN_SECONDS:
            try:
                lines, streak = vm.new_lines(), 0
            except Exception as error:
                lines, streak = [], streak + 1
                failed("logs", error, streak)
            for line in lines:
                say(line)
            empty = 0 if lines else empty + 1
            if empty < DRAIN_EMPTY_READS:
                sleep(interval)

    started, status, shown = clock(), Status(None, None, None), {}
    poll_streak = log_streak = 0
    try:
        vm.start(job, token)
        while True:
            try:
                status, poll_streak = vm.poll(), 0
            except Exception as error:
                poll_streak += 1
                failed("status", error, poll_streak)
            for label, value in (("cluster", status.cluster), ("task", status.task)):
                if value is not None and shown.get(label) != value:
                    shown[label] = value
                    say(f"{label}: {value}")
            if status.code is not None or status.cluster in ("stopped", "error"):
                drain()
                if status.code is None:
                    say(f"the cluster is {status.cluster} and the task has no exit code")
                return 1 if status.code is None else status.code
            running = status.task in RUNNING_TASK
            if running:
                try:
                    for line in vm.new_lines():
                        say(line)
                    log_streak = 0
                except Exception as error:
                    log_streak += 1
                    failed("logs", error, log_streak)
            if running and max(poll_streak, log_streak) >= MAX_FAILURES:
                say(f"giving up: {max(poll_streak, log_streak)} calls in a row failed while the task runs")
                return 1
            if clock() - started >= job.minutes * 60:
                say(f"the time limit of {job.minutes} minutes passed; stopping the VM")
                return TIME_LIMIT_CODE
            sleep(interval)
    except Exception as error:
        say(f"{type(error).__name__}: {error}")
        return 1
    finally:
        vm.stop(job.name)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--" not in argv:
        raise SystemExit("give the script and its arguments after --")
    own, command = argv[: argv.index("--")], argv[argv.index("--") + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vm-type", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--disk", type=int, required=True, help="GiB")
    parser.add_argument("--name", required=True, help="a prefix; each launch adds a timestamp and a random suffix")
    parser.add_argument("--max-minutes", type=int, default=90, help="the VM stops when the run passes this")
    parser.add_argument("--token-file", default="~/.arraylake/atera-ingest-token")
    parser.add_argument("--dry-run", action="store_true", help="print the command and contact nothing")
    args = parser.parse_args(own)
    if not command or not (HERE / command[0]).is_file():
        raise SystemExit(f"{command[0] if command else 'no script'} is not a script in {HERE.name}/")
    token = read_token(Path(args.token_file).expanduser())
    job = Job(unique_name(args.name), args.vm_type, args.region, args.workspace, args.disk, args.max_minutes,
              command[0], command[1:])
    if args.dry_run:
        print(call_text(job))
        print(remote_script(job), end="")
        return 0
    install_stop_signals()
    print(f"launching {job.name}: {job.vm_type} in {job.region}, at most {job.minutes} minutes", flush=True)
    return run(CoiledVm(), job, token, out=lambda line: print(line, flush=True))


if __name__ == "__main__":
    raise SystemExit(main())
