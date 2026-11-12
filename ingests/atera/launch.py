"""Run a script, or the full ingest and its verification, on one Coiled VM from a clean copy of the committed code.

    launch.py --vm-type m7g.xlarge --region us-west-2 --workspace earthmover-devs --disk 100 --name atera-measure \
        -- measure.py --part all ...
    launch.py --vm-type m7g.xlarge --region us-west-2 --workspace earthmover-devs --name atera-kidney --ingest \
        --sample kidney --repo arraylake://<org>/atera-kidney --catalog arraylake://<org> --namespace <ns> \
        --branch trial-<name> --concurrency 8 --max-minutes 600
    launch.py --attach <cluster id or name> --workspace earthmover-devs

The VM builds its own environment from ``uv.lock`` inside a Linux container, never from the laptop's environment.
The launcher stops the VM when the script ends, fails or passes the time limit. On an interrupt the script form
stops the VM; the ingest form and an attach do not. It exits with the script's exit code. The Arraylake token never appears in any output.
The ingest form writes a trial branch only. Its last step moves ``main`` to the snapshot that the verification read.
With ``--gates`` the verification runs a part of the gates. Then the caller gives ``--no-promote`` or ``--promote-after-gates``.
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

import fetch
from sources import BUNDLE, HE_ALIGNMENT, HE_IMAGE, SAMPLES

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
#: Consecutive failed status calls, while the task runs, that end the run: 30 polls of 10 s are five minutes.
#: Before the task runs no failure counts.
MAX_FAILURES = 30
#: Task states of a batch job, as ``coiled batch wait`` counts them: ``assigned`` and ``running`` both mean the VM works.
RUNNING_TASK = ("assigned", "running")
#: The longest the launcher waits for the cluster to report ``stopping`` or ``stopped`` after the stop request.
STOP_SECONDS = 60
#: The disk beyond the downloads, in GiB: 10 for the scratch Parquet file of the largest transcript tile (2,230,658,968
#: bytes measured), 10 for the uv environment and its cache, 10 for the image and the system, 10 of margin.
OVERHEAD_GIB = 40
#: The part of the overhead that the data directory needs after the environment exists: the tile's scratch file.
TILE_SCRATCH_GIB = 10
#: The default disk of the ingest form is the requirement rounded up to a multiple of this.
DISK_STEP_GIB = 25
GIB = 1 << 30
#: The branch that the ingest form writes; ``promote.py`` moves ``main`` to its snapshot.
TRIAL_BRANCH = re.compile(r"trial-[A-Za-z0-9._-]+")
#: What each exit code of ``check_bundle.py`` means, for the line that follows a failed check.
BUNDLE_CODES = {1: "the bundle differs from the record", 2: "the record cannot serve as a reference",
                3: "the bundle does not open", 4: "the check did not finish"}
#: The elements and the gates of a remote bundle: each reads few bytes of the source by range request.
REMOTE_ELEMENTS = ("root",)
REMOTE_GATES = ("complete", "attrs", "source")
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
class IngestPlan:
    """What ``ingest.py --mode full`` and ``verify.py --mode full`` take, and the shape of the run.

    With ``remote_bundle`` the run downloads nothing: each step reads the sample's objects by range request.
    ``promote`` says whether the last step moves ``main`` to the snapshot that the verification read.
    ``promote_after_gates`` says that the caller accepts this move after a part of the gates.
    """

    sample: str
    repo: str
    catalog: str
    namespace: str
    branch: str
    concurrency: int
    elements: str | None
    gates: str | None
    replace_tables: str | None
    remote_bundle: bool = False
    promote: bool = True
    promote_after_gates: bool = False
    root_keys: str | None = None


@dataclass(frozen=True)
class Job:
    """One launch. The token is not part of it.

    ``minutes`` is the launcher's time limit, ``None`` for none. ``status_failures`` is the number of failed status
    calls in a row, while the task runs, that end the run; ``None`` means never, so a lost network does not end the job.
    ``stops`` says when the launcher stops the VM: ``always`` on every exit path (a script), ``viewer`` only when the
    start fails, at the time limit and when the job ends (the ingest), ``never`` (an attached launcher).
    """

    name: str
    vm_type: str
    region: str
    workspace: str
    disk: int
    minutes: int | None
    script: str
    arguments: list[str]
    ingest: IngestPlan | None = None
    status_failures: int | None = MAX_FAILURES
    stops: str = "always"


def default_branch(now: datetime | None = None) -> str:
    """The trial branch of a run that gives none: ``trial-`` and the UTC date."""
    return f"trial-{now or datetime.now(timezone.utc):%Y%m%d}"


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


def required_disk_gib(sample: str, remote_bundle: bool = False) -> int:
    """The disk, in GiB, that the ingest of ``sample`` needs: its downloads, rounded up, and ``OVERHEAD_GIB``.

    A remote bundle has no download, so it needs the overhead only.
    """
    return OVERHEAD_GIB if remote_bundle else -(-download_bytes(sample) // GIB) + OVERHEAD_GIB


def download_bytes(sample: str) -> int:
    return sum(o["size"] for o in fetch.wanted(SAMPLES[sample]))


def default_disk_gib(sample: str, remote_bundle: bool = False) -> int:
    return -(-required_disk_gib(sample, remote_bundle) // DISK_STEP_GIB) * DISK_STEP_GIB


def names_fault(flag: str, text: str) -> str | None:
    """The fault of ``text`` unless it is names with no empty one and no whitespace, comma-separated."""
    if any(not name or re.search(r"\s", name) for name in text.split(",")):
        return f"--{flag} is names separated by commas, each non-empty and with no whitespace: {text!r}"
    return None


def plan_fault(elements: str | None, gates: str | None, remote_bundle: bool, promote: bool,
               promote_after_gates: bool, root_keys: str | None) -> str | None:
    """The rule that these options of the ingest form break, or ``None``.

    A part of the gates moves ``main`` only when the caller says so. A remote bundle reads few bytes only.
    """
    for flag, text in (("elements", elements), ("gates", gates), ("root-keys", root_keys)):
        if text is not None and (fault := names_fault(flag, text)):
            return fault
    if promote_after_gates and not gates:
        return "--promote-after-gates needs --gates"
    if promote_after_gates and not promote:
        return "--promote-after-gates and --no-promote exclude each other"
    if gates and promote and not promote_after_gates:
        return "--gates runs a part of the verification; give --no-promote or --promote-after-gates"
    if remote_bundle:
        if not elements or not gates:
            return "--remote-bundle needs --elements and --gates, so that the run reads few bytes by range request"
        for flag, text, allowed in (("elements", elements, REMOTE_ELEMENTS), ("gates", gates, REMOTE_GATES)):
            if other := [name for name in text.split(",") if name not in allowed]:
                return f"--remote-bundle takes only the {flag} {', '.join(allowed)}, not {', '.join(other)}"
    if root_keys is not None and elements != "root":
        return "--root-keys needs --elements root alone"
    return None


ENVIRONMENT = """test -f uv.lock || { echo "the uploaded code is missing from $PWD"; exit 2; }
# The environment lives outside the working directory, which Coiled syncs.
export UV_PROJECT_ENVIRONMENT=/opt/venv UV_CACHE_DIR=/tmp/uv-cache
"""


def remote_script(job: Job) -> str:
    """The shell script that the VM runs: build the environment from the lockfile, then run the job's steps."""
    if job.ingest:
        return ingest_script(job.ingest)
    run = shlex.join(["uv", "run", "--frozen", "--package", "atera-ingest", "python",
                      f"ingests/atera/{job.script}", *job.arguments])
    return f"""#!/bin/bash
set -euo pipefail
{ENVIRONMENT}echo "machine: $(uname -m)"
uv sync --frozen --package atera-ingest
exec {run}
"""


def ingest_script(plan: IngestPlan) -> str:
    """The shell script of the ingest form.

    The steps are environment, preflight, bundle-remote, space, download, check, bundle-local, ingest, verify, promote.
    A remote bundle has no space, download, check and bundle-local step. Each step prints a start line and its wall time.
    The first failed step ends the script with its exit code and prints its last 40 lines again.
    So a failed bundle check reaches no download and no ingest, and a failed verification reaches no promote.
    The verification writes the snapshot that it read to a file, and the promote step moves ``main`` to that one only.
    """
    if not TRIAL_BRANCH.fullmatch(plan.branch):
        raise ValueError(f"the ingest form writes a branch trial-<name>, not {plan.branch!r}")
    if fault := plan_fault(plan.elements, plan.gates, plan.remote_bundle, plan.promote, plan.promote_after_gates,
                           plan.root_keys):
        raise ValueError(fault)
    quote = shlex.quote
    python = "uv run --frozen --package atera-ingest python ingests/atera"
    common = f"--sample {quote(plan.sample)} --repo {quote(plan.repo)} --catalog {quote(plan.catalog)}"
    # One argument list for the preflight and the ingest, so they cannot differ; the ingest adds the local files.
    arguments = (f'{common} --namespace {quote(plan.namespace)} --mode full --branch {quote(plan.branch)} '
                 f'--concurrency {plan.concurrency}'
                 + (f" --elements {quote(plan.elements)}" if plan.elements else "")
                 + (f" --replace-tables {quote(plan.replace_tables)}" if plan.replace_tables else "")
                 + (f" --root-keys {quote(plan.root_keys)}" if plan.root_keys else ""))
    if plan.remote_bundle:
        local = head = ""
    else:
        files = {kind: f"$DATA/{o['name']}" for kind, suffix in (("bundle", BUNDLE), ("image", HE_IMAGE),
                                                              ("alignment", HE_ALIGNMENT))
                 for o in fetch.wanted(SAMPLES[plan.sample]) if o["name"].endswith(suffix)}
        local = (f' --bundle "{files["bundle"]}" --he-image "{files["image"]}" '
                 f'--he-alignment "{files["alignment"]}"')
        head = " --head-source"
    ingest = f"{python}/ingest.py {arguments}{local}"
    verify = (f'{python}/verify.py {common} --mode full --branch {quote(plan.branch)}{local}{head}'
              + (f" --gates {quote(plan.gates)}" if plan.gates else "") + ' --snapshot-file "$SNAPSHOT"')
    # A description is one shell word, so no text of the plan runs as a command.
    verification = quote(f"the verification, only the gates {plan.gates}" if plan.gates else "the full verification")
    written = quote((f"the ingest of the elements {plan.elements} only" if plan.elements else "the full ingest")
                    + (", by range request" if plan.remote_bundle else ", from the local files")
                    + f", to the branch {plan.branch}; the first write to Arraylake")
    moved = quote(f"move main forward to the snapshot that the verification read on the branch {plan.branch}"
                  + (f", after the gates {plan.gates} only" if plan.promote_after_gates else ""))
    codes = "\n".join(f'    {code}) echo "=== bundle check: exit code {code}: {meaning}" ;;'
                      for code, meaning in BUNDLE_CODES.items())
    need_kb = (download_bytes(plan.sample) + TILE_SCRATCH_GIB * GIB) // 1024
    head_of_script = f"""#!/bin/bash
set -uo pipefail
{ENVIRONMENT}DATA="${{ATERA_DATA:-/tmp/atera-data}}"
LOGS="${{ATERA_LOGS:-/tmp/atera-steps}}"
SNAPSHOT="$DATA/verified-snapshot"
mkdir -p "$DATA" "$LOGS"

# step NAME DESCRIPTION COMMAND...: print a start line, run, print the wall time; on failure print the last 40 lines and exit.
step() {{
  local name=$1 description=$2
  shift 2
  echo "=== step $name: start: $description"
  local began=$SECONDS
  "$@" 2>&1 | tee "$LOGS/$name.log"
  local code=${{PIPESTATUS[0]}}
  if [ "$code" -ne 0 ]; then
    echo "=== step $name: FAILED with exit code $code after $((SECONDS - began)) s"
    echo "=== last 40 lines of step $name"
    tail -n 40 "$LOGS/$name.log"
    exit "$code"
  fi
  echo "=== step $name: done in $((SECONDS - began)) s"
}}

# Compare a bundle with the sample's record; a line says what a non-zero exit code means.
bundle_check() {{
  {python}/check_bundle.py --sample {quote(plan.sample)} "$@"
  local code=$?
  case "$code" in
    0) ;;
{codes}
    *) echo "=== bundle check: exit code $code: not a code of check_bundle.py" ;;
  esac
  return "$code"
}}

echo "machine: $(uname -m)"
step environment "build the environment from uv.lock" uv sync --frozen --package atera-ingest
step preflight "the arguments, the repo, the catalog and each table's state, with no write and no bundle" {python}/ingest.py {arguments} --preflight
step bundle-remote "the sample's objects against the record, by range request, with no download" bundle_check
"""
    download = f"""# The data directory must hold the downloads and the largest tile's scratch file.
check_space() {{
  local free_kb
  free_kb=$(df -Pk "$DATA" | awk 'NR==2 {{print $4}}')
  case "$free_kb" in
    ''|*[!0-9]*) echo "the free space of $DATA is not a number: '$free_kb'"; return 3 ;;
  esac
  echo "free space in $DATA: $free_kb KiB; needed: {need_kb} KiB"
  if [ "$free_kb" -lt {need_kb} ]; then
    echo "not enough free space in $DATA for the downloads and the largest tile's scratch file"
    return 3
  fi
}}

echo "=== disk before the download: the data directory, /opt (the environment) and the working directory"
df -h "$DATA" /opt . || true
step space "free space of the data directory against the downloads and the tile's scratch file" check_space
step download "one streaming GET for each object of the sample, anonymous, resumed up to 5 times" \\
  {python}/fetch.py download --sample {quote(plan.sample)} --directory "$DATA"
echo "=== disk after the download"
df -h "$DATA" || true
step check "size of each file, and its ETag where the MD5 of the file or of its parts can be recomputed" \\
  {python}/fetch.py check --sample {quote(plan.sample)} --directory "$DATA"
step bundle-local "the local files against the record" bundle_check{local}
step ingest {written} {ingest}
echo "=== disk after the ingest"
df -h "$DATA" || true
echo "=== note: the verification reads the local files; its source gate sends HEAD requests to the real objects"
"""
    remote = f"""step ingest {written} {ingest}
"""
    promote = f"""
# Move main to the snapshot that the verification read. An absent or empty file moves nothing.
promote_verified() {{
  local snapshot=""
  [ -f "$SNAPSHOT" ] && snapshot=$(head -n 1 "$SNAPSHOT" | tr -d '[:space:]')
  if [ -z "$snapshot" ]; then
    echo "the verification recorded no snapshot in $SNAPSHOT; main stays as it is"
    return 1
  fi
  {python}/promote.py --repo {quote(plan.repo)} --from {quote(plan.branch)} --expect="$snapshot"
}}

step promote {moved} promote_verified
""" if plan.promote else 'echo "=== no promote step: main stays as it is"\n'
    # A snapshot file of an earlier run must not reach the promote step.
    return f"""{head_of_script}{remote if plan.remote_bundle else download}rm -f "$SNAPSHOT"
step verify {verification} {verify}
{promote}echo "=== all steps passed"
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

    def find_by_name(self, name: str) -> int | None:
        """The id of the cluster called ``name``, or ``None`` when the service knows none."""
        import coiled

        try:
            jobs = coiled.batch.status(cluster=name, workspace=self.workspace)
        except Exception:
            return None
        return jobs[0]["cluster_id"] if isinstance(jobs, list) and jobs else None

    def stop(self, name: str) -> None:
        """Stop the cluster by id, or by name when the start failed; print the name and the state after."""
        import coiled
        from coiled.errors import DoesNotExist

        by_name = not self.cluster_id
        if by_name:
            self.cluster_id = self.find_by_name(name)
        failure, gone = None, False
        try:
            target = {"cluster_id": self.cluster_id} if self.cluster_id else {"name": name}
            coiled.delete_cluster(**target, workspace=self.workspace)
        except DoesNotExist:
            gone = True
        except Exception as error:
            failure = error
        if by_name and not self.cluster_id:
            self.cluster_id = self.find_by_name(name)
        if gone and not self.cluster_id:
            state = "absent"
        elif not self.cluster_id:
            # Nothing found by name and the delete did not say it was absent: a person must look.
            state = "unknown (no cluster found by name)"
        else:
            # The state read right after the request is the old one, so wait for it to change.
            state = "stopped" if gone else self.state_after_stop()
        print(f"cluster {name}: {state}", flush=True)
        if failure is not None and by_name and not self.cluster_id:
            print(f"could not stop the cluster {name}: {type(failure).__name__}: {failure}; stop it by hand: "
                  f"coiled cluster list --workspace {self.workspace}, then coiled cluster stop {name} "
                  f"--workspace {self.workspace}", flush=True)
        elif state not in ("stopping", "stopped", "absent"):
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


class AttachedVm(CoiledVm):
    """A batch job that another launcher started: it serves the last ``tail`` lines of its log, then the new ones."""

    def __init__(self, ref: str, tail: int, **kwargs):
        super().__init__(**kwargs)
        self.ref, self.tail, self.backlog = ref, tail, None

    def start(self, job: Job, token: str) -> None:
        import coiled

        self.workspace = job.workspace
        jobs = coiled.batch.status(cluster=int(self.ref) if self.ref.isdigit() else self.ref, workspace=self.workspace)
        if not isinstance(jobs, list) or not jobs:
            raise StatusError(f"no batch job for {self.ref}: {str(jobs)[:200]}")
        self.cluster_id, self.job_id = jobs[0]["cluster_id"], jobs[0]["id"]
        self.backlog = super().new_lines()[-self.tail:] if self.tail else []

    def new_lines(self) -> list[str]:
        lines = super().new_lines()
        if self.backlog is not None:
            lines, self.backlog = [*self.backlog, *lines], None
        return lines


def follow_lines(job: Job, vm) -> tuple[str, str]:
    """How to follow the job again and how to stop it."""
    ident = job.name if job.stops == "never" else (getattr(vm, "cluster_id", None) or job.name)
    return (f"follow: launch.py --attach {ident} --workspace {job.workspace}",
            f"stop: launch.py --stop {ident} --workspace {job.workspace}")


def run(vm, job: Job, token: str, *, out: Callable[[str], object] = print, clock=None, sleep=None,
        interval: float = 10) -> int:
    """Start ``job`` on ``vm``, forward its lines, and return its exit code.

    ``job.stops`` says on which paths the VM stops. Nothing asks for the log before a task runs, and a failed call
    is one line and the next poll tries again.
    """

    # Looked up at the call, so a test can replace ``time.sleep``.
    clock, sleep = clock or time.monotonic, sleep or time.sleep

    def say(text):
        out(text.replace(token, "***"))

    def failed(what, error, streak):
        if streak == 1 or streak % 10 == 0:
            more = f" ({streak} in a row; the job runs on)" if what == "status" and job.status_failures is None else ""
            say(f"{what}: {type(error).__name__}: {error}{more}")

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
    stop, job_known = job.stops == "always", False
    try:
        try:
            vm.start(job, token)
        except BaseException:
            # A failed start leaves what it created, and no job to follow.
            stop = job.stops != "never"
            raise
        job_known = True
        if job.stops == "viewer":
            for line in follow_lines(job, vm):
                say(line)
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
                stop = job.stops != "never"
                if not stop:
                    say(follow_lines(job, vm)[1] + " (the cluster is not stopped)")
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
            # A failed log read never ends the run: the lines stay in Coiled's logs.
            if running and job.status_failures is not None and poll_streak >= job.status_failures:
                say(f"giving up: {poll_streak} status calls in a row failed while the task runs")
                return 1
            if job.minutes is not None and clock() - started >= job.minutes * 60:
                say(f"the time limit of {job.minutes} minutes passed; stopping the VM")
                stop = job.stops != "never"
                return TIME_LIMIT_CODE
            sleep(interval)
    except KeyboardInterrupt:
        if job.stops == "viewer" and job_known:
            for line in follow_lines(job, vm):
                say(line)
        raise
    except Exception as error:
        say(f"{type(error).__name__}: {error}")
        return 1
    finally:
        if stop:
            vm.stop(job.name)


def plan_text(job: Job) -> str:
    """What the ingest form downloads, with the recorded facts, and the disk arithmetic."""
    objects = fetch.wanted(SAMPLES[job.ingest.sample])
    verb = "range requests to" if job.ingest.remote_bundle else "download"
    lines = [f"{verb} {o['name']}: {o['size']:,} bytes, ETag {o['etag']}" for o in objects]
    total = sum(o["size"] for o in objects)
    if job.ingest.remote_bundle:
        lines.append(f"disk: no download occurs; {OVERHEAD_GIB} GiB of overhead at least; this launch has {job.disk} GiB")
    else:
        lines.append(f"disk: {total:,} bytes of downloads are {-(-total // GIB)} GiB, plus {OVERHEAD_GIB} GiB of "
                     f"overhead is {required_disk_gib(job.ingest.sample)} GiB at least; this launch has {job.disk} GiB")
    return "\n".join(lines)


ARRAYLAKE = "arraylake://"
NOTE = (
    "With --ingest the launcher is a viewer of the job. It never stops the VM on Ctrl-C, SIGTERM, SIGHUP, a closed "
    "terminal or a status error, except at its own --max-minutes limit and when the start fails; it stops the VM "
    "when the job ends. A dead launcher does not stop the job: the job ends by itself, at --max-minutes on the "
    "server, or when a person runs launch.py --stop <cluster id> --workspace <ws>. Follow a job again with "
    "launch.py --attach <cluster id> --workspace <ws>; an attached launcher never stops the cluster. A script "
    "(after --) stops the VM on every exit path. The ingest form writes the branch trial-<name>, never main. Its "
    "last step, promote.py, moves main forward to the snapshot that the verification read. The step runs only after "
    "the verify step passed, and it refuses a trial branch that holds a later commit. With --gates the verify step "
    "runs a part of the gates. So the launcher refuses --gates unless the run has exactly one of --no-promote and "
    "--promote-after-gates."
)




def check_ingest(args, fail) -> None:
    """Exit unless the ingest form's arguments are consistent; nothing has been uploaded or launched yet."""
    repo = re.fullmatch(rf"{ARRAYLAKE}([^/\s]+)/([^/\s]+)", args.repo)
    catalog = re.fullmatch(rf"{ARRAYLAKE}([^/\s]+)", args.catalog)
    if not repo:
        fail(f"--repo is arraylake://<org>/<repo>, not {args.repo!r}")
    if not catalog:
        fail(f"--catalog is arraylake://<org>, not {args.catalog!r}")
    if repo[1] != catalog[1]:
        fail(f"--repo and --catalog must name the same org, not {repo[1]!r} and {catalog[1]!r}")
    if args.namespace != f"{repo[2]}-tables":
        fail(f"--namespace for the repo {repo[2]} is {repo[2]}-tables, not {args.namespace!r}")
    if not TRIAL_BRANCH.fullmatch(args.branch):
        fail(f"--branch is trial-<name>, not {args.branch!r}; the last step of the run moves main")
    if args.sample not in SAMPLES:
        fail(f"--sample is one of {', '.join(sorted(SAMPLES))}, not {args.sample!r}")
    if SAMPLES[args.sample].repo != repo[2]:
        fail(f"--sample {args.sample} is the repo {SAMPLES[args.sample].repo}, not {repo[2]}")
    if args.concurrency < 1:
        fail(f"--concurrency is at least 1, not {args.concurrency}")
    for flag in ("elements", "gates", "replace_tables"):
        if (text := getattr(args, flag)) is not None:
            if fault := names_fault(flag.replace("_", "-"), text):
                fail(fault)
    if args.elements is not None:
        # Only this check needs the ingest's modules, and their import is slow.
        from ingest import ELEMENTS, select_elements

        try:
            select_elements(args.elements, ELEMENTS["full"])
        except ValueError as error:
            fail(f"--elements: {error}")
    if args.gates is not None:
        # Only this check needs the verification's modules, and their import is slow.
        from verify import GATES, select_gates

        try:
            select_gates([args.gates], GATES["full"])
        except ValueError as error:
            fail(f"--gates: {error}")
    if fault := plan_fault(args.elements, args.gates, args.remote_bundle, not args.no_promote, args.promote_after_gates,
                           args.root_keys):
        fail(fault)
    if args.root_keys is not None:
        # Only this check needs the ingest's modules, and their import is slow.
        from ingest import root_key_names

        try:
            root_key_names(args.root_keys, [args.elements])
        except ValueError as error:
            fail(f"--root-keys: {error}")


def parse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """The options and the command after ``--``; each form's missing or conflicting option exits."""
    own, command = (argv[: argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=NOTE)
    for flag in ("vm-type", "region", "workspace", "name", "sample", "repo", "catalog", "namespace",
                 "replace-tables", "attach", "stop"):
        parser.add_argument(f"--{flag}")
    parser.add_argument("--elements",
                        help="With --ingest, ingest.py writes only these elements, separated by commas. "
                             "With --remote-bundle the only element is root.")
    parser.add_argument("--root-keys",
                        help="With --ingest and --elements root alone, ingest.py writes only these root attributes, "
                             "separated by commas. Another root attribute that changes ends the ingest before the write.")
    parser.add_argument("--gates",
                        help="With --ingest, verify.py runs only these gates, separated by commas. The launcher "
                             "refuses --gates unless the run has exactly one of --no-promote and "
                             "--promote-after-gates. With --remote-bundle each gate is one of complete, attrs, source.")
    parser.add_argument("--branch", help="with --ingest, the branch to write: trial-<name>; trial-<UTC date> by default")
    parser.add_argument("--disk", type=int, help="GiB; the ingest form defaults to its requirement, rounded up")
    parser.add_argument("--max-minutes", type=int, help="the VM stops when the run passes this; 90 for a script")
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--status-failures", type=int,
                        help="failed status calls in a row that end the run while the task runs; 30 for a script, "
                             "never for --ingest")
    parser.add_argument("--tail", type=int, default=40, help="with --attach, the last log lines to show first")
    parser.add_argument("--token-file", default="~/.arraylake/atera-ingest-token")
    parser.add_argument("--ingest", action="store_true",
                        help="run ingest.py and verify.py, both --mode full, on a trial branch, then promote.py")
    parser.add_argument("--remote-bundle", action="store_true",
                        help="With --ingest, download nothing: each step reads the sample's objects by range request. "
                             "The launcher refuses --remote-bundle without --elements and --gates. The only element "
                             "is root, and each gate is one of complete, attrs, source.")
    parser.add_argument("--no-promote", action="store_true", help="with --ingest, leave main as it is")
    parser.add_argument("--promote-after-gates", action="store_true",
                        help="with --ingest and --gates, move main after only the gates of --gates passed")
    parser.add_argument("--dry-run", action="store_true", help="print the command and contact nothing")
    args = parser.parse_args(own)

    def fail(message):
        raise SystemExit(f"launch.py: error: {message}")

    def need(*flags):
        for flag in flags:
            if getattr(args, flag.replace("-", "_")) is None:
                fail(f"--{flag} is needed" + (" for --ingest" if args.ingest else ""))

    if args.max_minutes is not None and args.max_minutes < 1:
        fail(f"--max-minutes is at least 1, not {args.max_minutes}")
    for flag in ("replace-tables", "root-keys"):
        if getattr(args, flag.replace("-", "_")) is not None and not args.ingest:
            fail(f"--{flag} belongs to --ingest")
    for flag in ("remote-bundle", "no-promote", "promote-after-gates"):
        if getattr(args, flag.replace("-", "_")) and not args.ingest:
            fail(f"--{flag} belongs to --ingest")
    if args.attach or args.stop:
        need("workspace")
    elif args.ingest:
        if command:
            fail("--ingest takes no script; the script form is: options -- script arguments")
        need("vm-type", "region", "workspace", "name", "max-minutes", "sample", "repo", "catalog", "namespace",
             "concurrency")
        args.branch = default_branch() if args.branch is None else args.branch
        check_ingest(args, fail)
        needed = required_disk_gib(args.sample, args.remote_bundle)
        if args.disk is not None and args.disk < needed:
            fail(f"--disk {args.disk} is below the {needed} GiB that the ingest of {args.sample} needs: "
                 f"{'' if args.remote_bundle else 'its downloads and '}{OVERHEAD_GIB} GiB of overhead")
    else:
        need("vm-type", "region", "workspace", "name", "disk")
        if not command or not (HERE / command[0]).is_file():
            fail(f"{command[0] if command else 'no script'} is not a script in {HERE.name}/; "
                 "give the script and its arguments after --")
    return args, command


def stop_cluster(ref: str, workspace: str, vm=None, dry_run: bool = False) -> int:
    """Print the name and the state of the cluster ``ref`` (id or name), delete it, and print the state after.

    With ``dry_run`` it prints what it would delete and deletes nothing.
    """
    import coiled
    import coiled.v2

    vm = vm or CoiledVm()
    vm.workspace = workspace
    if ref.isdigit():
        vm.cluster_id = int(ref)
    else:
        jobs = coiled.batch.status(cluster=ref, workspace=workspace)
        if not isinstance(jobs, list) or not jobs:
            raise SystemExit(f"launch.py: error: no cluster {ref!r} in {workspace}")
        vm.cluster_id = jobs[0]["cluster_id"]
    details = coiled.v2.cluster_details(vm.cluster_id, workspace=workspace)
    state = details["current_state"]["state"]
    if dry_run:
        print(f"would stop cluster {details['name']} (id {vm.cluster_id}), now {state}; nothing was deleted", flush=True)
        return 0
    print(f"cluster {details['name']} (id {vm.cluster_id}): {state}", flush=True)
    vm.stop(details["name"])
    return 0


def interruptible(call: Callable[[], int]) -> int:
    """Run ``call``; an interrupt ends with the shell's code 130 and no traceback, after ``run`` has done its part."""
    try:
        return call()
    except KeyboardInterrupt:
        return 130


def main(argv: list[str] | None = None) -> int:
    args, command = parse(sys.argv[1:] if argv is None else argv)
    if args.stop:
        return stop_cluster(args.stop, args.workspace, dry_run=args.dry_run)
    if args.attach and args.dry_run:
        print(f"would follow {args.attach} in {args.workspace}: the last {args.tail} lines, then the new ones; "
              "it would stop nothing", flush=True)
        return 0
    if args.attach:
        token = read_token(Path(args.token_file).expanduser())
        job = Job(args.attach, "", "", args.workspace, 0, None, "", [], status_failures=None, stops="never")
        install_stop_signals()
        print(f"attaching to {args.attach}; the last {args.tail} lines first", flush=True)
        return interruptible(lambda: run(AttachedVm(args.attach, args.tail), job, token,
                                         out=lambda line: print(line, flush=True)))
    name = unique_name(args.name)
    if args.ingest:
        plan = IngestPlan(args.sample, args.repo, args.catalog, args.namespace, args.branch, args.concurrency,
                          args.elements, args.gates, args.replace_tables, args.remote_bundle, not args.no_promote,
                          args.promote_after_gates, args.root_keys)
        job = Job(name, args.vm_type, args.region, args.workspace,
                  args.disk or default_disk_gib(args.sample, args.remote_bundle),
                  args.max_minutes, "ingest", [], plan, args.status_failures, "viewer")
    else:
        job = Job(name, args.vm_type, args.region, args.workspace, args.disk, args.max_minutes or 90, command[0],
                  command[1:], None, MAX_FAILURES if args.status_failures is None else args.status_failures)
    if args.dry_run:
        if job.ingest:
            print(plan_text(job))
        print(call_text(job))
        print(remote_script(job), end="")
        return 0
    # The dry run shows stars for the secret, so only a launch reads the token.
    token = read_token(Path(args.token_file).expanduser())
    install_stop_signals()
    print(f"launching {job.name}: {job.vm_type} in {job.region}, at most {job.minutes} minutes", flush=True)
    return interruptible(lambda: run(CoiledVm(), job, token, out=lambda line: print(line, flush=True)))


if __name__ == "__main__":
    raise SystemExit(main())
