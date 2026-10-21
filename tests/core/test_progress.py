"""A long job says what it is doing; a short one says nothing.

The failure this guards is a process that prints nothing for three minutes,
which looks exactly like one that has hung. The reflex fix is a shorter
timeout, which turns an unreadable wait into an unexplained failure. Reporting
on a time interval is what separates the two, because a count threshold cannot
know how long its own work takes.
"""

from __future__ import annotations

import io

from minus80.core.progress import Progress, _clock


def _run(total, steps, *, every, sleep=0.0):
    """Drive a Progress to completion against a fake clock, and return its output."""
    out = io.StringIO()
    now = [0.0]
    with Progress("walking directories", total, unit="pages",
                  every=every, stream=out) as note:
        note._started = note._last = 0.0
        import minus80.core.progress as mod
        real = mod.time.monotonic
        mod.time.monotonic = lambda: now[0]
        try:
            for _ in range(steps):
                now[0] += sleep
                note.advance()
        finally:
            mod.time.monotonic = real
    return out.getvalue()


def test_a_fast_job_says_what_it_is_doing_and_nothing_more():
    """No interval elapses, so there is no progress line and no epilogue."""
    text = _run(100, 100, every=5.0, sleep=0.0)
    assert text.strip() == "walking directories (100 pages)"


def test_a_slow_job_reports_on_the_interval_with_a_denominator():
    text = _run(100, 10, every=5.0, sleep=2.0)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    assert lines[0] == "walking directories (100 pages)"
    assert any("of 100 pages" in ln and "%" in ln for ln in lines[1:])
    assert any("left" in ln for ln in lines[1:]), "a denominator should give an estimate"
    assert "in " in lines[-1], "a job that reported should say what it finished"


def test_without_a_total_it_still_shows_movement():
    """No denominator, so no percentage -- but enough to tell moving from stuck."""
    text = _run(None, 10, every=5.0, sleep=2.0)
    assert "so far" in text
    assert "%" not in text


def test_the_label_comes_before_the_numbers():
    """A fraction alone does not say what is being counted."""
    text = _run(100, 10, every=5.0, sleep=2.0)
    assert text.splitlines()[0].strip().startswith("walking directories")


def test_a_scaled_unit_counts_exactly_and_shows_readably():
    out = io.StringIO()
    with Progress("fetching", 4 << 20, unit="MiB", scale=1 << 20, stream=out):
        pass
    assert "4.0 MiB" in out.getvalue()


def test_durations_read_at_a_glance():
    assert _clock(42) == "42s"
    assert _clock(600) == "10m"
    assert _clock(7200) == "2.0h"
