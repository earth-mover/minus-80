"""A long job says what it is doing; a short one says nothing.

The failure this guards is a process that prints nothing for three minutes,
which looks exactly like one that has hung. The reflex fix is a shorter
timeout, which turns an unreadable wait into an unexplained failure. Reporting
on a time interval is what separates the two, because a count threshold cannot
know how long its own work takes.
"""

import io
import time

import pytest

from minus80.core.progress import Progress


@pytest.fixture
def run(monkeypatch):
    """Drive a Progress against a fake clock and return what it printed."""
    def run(total, steps, *, seconds_per_step):
        now = [0.0]
        monkeypatch.setattr(time, "monotonic", lambda: now[0])
        out = io.StringIO()
        with Progress("walking directories", total, unit="pages", every=5.0, stream=out) as note:
            for _ in range(steps):
                now[0] += seconds_per_step
                note.advance()
        return [line.strip() for line in out.getvalue().splitlines()]
    return run


def test_fast_job_prints_only_its_label(run):
    assert run(100, 100, seconds_per_step=0.0) == ["walking directories (100 pages)"]


def test_slow_job_reports_share_and_time_left(run):
    assert run(100, 10, seconds_per_step=2.0) == [
        "walking directories (100 pages)",
        "3 of 100 pages, 3%, about 3m left",
        "6 of 100 pages, 6%, about 3m left",
        "9 of 100 pages, 9%, about 3m left",
        "10 pages in 20s",
    ]


def test_without_total_reports_elapsed(run):
    assert run(None, 10, seconds_per_step=2.0)[1] == "3, 6s so far"


def test_scaled_unit_shows_readably():
    out = io.StringIO()
    with Progress("fetching", 4 << 20, unit="MiB", scale=1 << 20, stream=out):
        pass
    assert out.getvalue().strip() == "fetching (4.0 MiB)"
