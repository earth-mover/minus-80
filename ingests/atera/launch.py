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
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
#: The container that builds the environment: uv and git on Debian, for x86 and ARM.
IMAGE = "ghcr.io/astral-sh/uv:python3.12-bookworm"
TOKEN_VARIABLE = "ARRAYLAKE_TOKEN"
TAGS = {"BillingCategory": "RnD"}
#: The exit code that means the launcher ended the run at the time limit; the shell's code for a timeout.
TIME_LIMIT_CODE = 124
#: Coiled prefixes each line of a batch task's output with this marker.
BATCH_MARKER = re.compile(r"^__BATCH \d+\.\d+__ ?")


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


def command_line(job: Job) -> str:
    """The ``coiled batch run`` command that is equivalent to the launch, with ``***`` for the secret."""
    arguments = [
        "coiled", "batch", "run", "--name", job.name, "--workspace", job.workspace, "--vm-type", job.vm_type,
        *(["--arm"] if is_arm(job.vm_type) else []), "--region", job.region, "--disk-size", str(job.disk),
        "--container", IMAGE, "--ignore-container-entrypoint", "true",
        "--secret-env", f"{TOKEN_VARIABLE}=***", *(a for k, v in TAGS.items() for a in ("--tag", f"{k}={v}")),
        "--job-timeout", f"{job.minutes} minutes", "--upload", "<git archive of HEAD>", "<remote script>",
    ]
    return shlex.join(arguments)


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

    def __init__(self):
        self.workspace = self.cluster_id = self.job_id = self.since = None
        self.seen: set = set()

    def start(self, job: Job, token: str) -> None:
        import coiled

        self.workspace = job.workspace
        # Coiled scans the script for local files to upload; from the export they are the committed ones.
        with code_export() as directory, contextlib.chdir(directory):
            found = coiled.batch.run(
                remote_script(job), command_as_script=True, name=job.name, workspace=job.workspace,
                container=IMAGE, ignore_container_entrypoint=True, secret_env={TOKEN_VARIABLE: token}, tag=TAGS,
                vm_type=[job.vm_type], arm=is_arm(job.vm_type), region=job.region, disk_size=str(job.disk),
                local_upload_path=str(directory), job_timeout=f"{job.minutes} minutes",
                cluster_kwargs={"timeout": job.minutes * 60},
            )
        self.cluster_id, self.job_id = found["cluster_id"], found["job_id"]

    def new_lines(self) -> list[str]:
        """The task's output lines that earlier calls did not return."""
        import coiled

        events = coiled.better_cluster_logs(
            cluster_id=self.cluster_id, workspace=self.workspace, dask=True, system=False,
            since_ms=self.since, filter=f"__BATCH {self.job_id}.")
        lines = []
        for event in events:
            key = (event["timestamp"], event.get("instance_id"), event["message"])
            if key in self.seen:
                continue
            self.seen.add(key)
            self.since = max(self.since or 0, event["timestamp"])
            lines += [BATCH_MARKER.sub("", line) for line in event["message"].splitlines()]
        return lines

    def exit_code(self) -> int | None:
        """The task's exit code, or ``None`` while it runs."""
        import coiled

        jobs = coiled.batch.status(cluster=self.cluster_id, workspace=self.workspace)
        if not jobs or not jobs[0]["tasks"]:
            return None
        if jobs[0].get("cluster_state") == "error":
            return 1
        task = jobs[0]["tasks"][0]
        if task["exit_code"] is not None:
            return task["exit_code"]
        return {"error": 1, "done": 0}.get(task["state"])

    def stop(self, name: str) -> None:
        import coiled

        try:
            coiled.delete_cluster(name=name, workspace=self.workspace)
        except Exception as error:
            print(f"could not stop the cluster {name}: {type(error).__name__}: {error}; stop it by hand", flush=True)


def run(vm, job: Job, token: str, *, out: Callable[[str], object] = print, clock=time.monotonic, sleep=time.sleep,
        interval: float = 10) -> int:
    """Start ``job`` on ``vm``, forward its lines, and return its exit code; the VM stops whatever happens."""

    def forward(lines):
        for line in lines:
            out(line.replace(token, "***"))

    started = clock()
    try:
        vm.start(job, token)
        while True:
            forward(vm.new_lines())
            code = vm.exit_code()
            if code is not None:
                # The log lags the task, so read once more after it ends.
                sleep(interval)
                forward(vm.new_lines())
                return code
            if clock() - started >= job.minutes * 60:
                out(f"the time limit of {job.minutes} minutes passed; stopping the VM")
                return TIME_LIMIT_CODE
            sleep(interval)
    except Exception as error:
        out(f"{type(error).__name__}: {error}".replace(token, "***"))
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
        print(command_line(job))
        print(remote_script(job), end="")
        return 0
    print(f"launching {job.name}: {job.vm_type} in {job.region}, at most {job.minutes} minutes", flush=True)
    return run(CoiledVm(), job, token, out=lambda line: print(line, flush=True))


if __name__ == "__main__":
    raise SystemExit(main())
