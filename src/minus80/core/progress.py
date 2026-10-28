"""Progress lines for a long job, printed often enough to tell working from hung.

An ingest can spend minutes inside one call, such as walking a TIFF directory
chain, and a process that prints nothing for three minutes looks the same as
one that has stalled.

:class:`Progress` reports on a time interval rather than every N items. A count
threshold cannot know how long its work takes: 100,000 pages is thirty seconds
of silence for one file and forty minutes for another. An interval reports on
the slow job and stays quiet about the fast one.

:class:`Heartbeat` covers what :class:`Progress` cannot: a job whose work
happens between counts, or inside one long call such as a commit. Progress
prints only when the job counts something, so a stage that counts nothing is
silent; a heartbeat prints from its own thread on a fixed interval, whatever
the job is doing.

Callers follow two rules:

- **Say what it is before saying how far.** "walking 829,080 directories" first,
  then the fractions. A fraction alone does not say what is being counted.
- **Give a denominator wherever one is known.** "300,000 of 829,080, 36%, about
  40 s left" tells you whether to wait; "300,000 so far" does not.
"""

from __future__ import annotations

import resource
import sys
import threading
import time
from collections.abc import Callable
from typing import Self


class Progress:
    """Report on an interval while a long job runs, and once when it finishes.

    ``total`` is the denominator where one is known; without it the line still
    reports what has been done and how long it has taken, which is enough to
    tell moving from stuck. ``unit`` names what is counted, and ``scale``
    divides it for display, so bytes can be counted exactly and shown in MiB.
    """

    def __init__(self, label: str, total: int | None = None, *,
                 unit: str = "", every: float = 5.0, scale: float = 1.0,
                 stream=None):
        self.label, self.total, self.unit = label, total, unit
        self.every, self.scale = every, scale
        self.stream = stream if stream is not None else sys.stdout
        self.done = 0
        self._started = self._last = time.monotonic()
        self._reported = False

    def __enter__(self) -> Self:
        known = f" ({self._amount(self.total)})" if self.total else ""
        print(f"  {self.label}{known}", file=self.stream, flush=True)
        return self

    def __exit__(self, *exc) -> None:
        # A job that finished before the first interval printed no progress
        # line, so it gets no closing line either.
        if self._reported and exc[0] is None:
            elapsed = time.monotonic() - self._started
            print(f"    {self._amount(self.done)} in {_clock(elapsed)}",
                  file=self.stream, flush=True)

    def advance(self, by: int = 1) -> None:
        """Count work done, and report if the interval has elapsed."""
        self.done += by
        now = time.monotonic()
        if now - self._last < self.every:
            return
        self._last = now
        self._reported = True
        elapsed = now - self._started
        line = f"    {self._figure(self.done)}"
        if self.total:
            share = self.done / self.total
            # The unit goes on the total only, so the line reads "747,279 of
            # 829,080 pages" rather than saying "pages" twice.
            line += f" of {self._amount(self.total)}, {share:.0%}"
            if share > 0:
                line += f", about {_clock(elapsed / share - elapsed)} left"
        else:
            line += f", {_clock(elapsed)} so far"
        print(line, file=self.stream, flush=True)

    def _figure(self, n: int) -> str:
        shown = n / self.scale
        return f"{shown:,.0f}" if self.scale == 1 else f"{shown:,.1f}"

    def _amount(self, n: int) -> str:
        return f"{self._figure(n)} {self.unit}".strip()


class Heartbeat:
    """Print ``describe()`` every ``every`` seconds from a background thread, until the block ends.

    ``describe`` runs on the heartbeat's thread, so it should only read
    counters the job updates, never wait on the job. It names the stage the
    job is in and its counters, so that a line read alone says where the job
    is.
    """

    def __init__(self, describe: Callable[[], str], *, every: float = 30.0, stream=None):
        self.describe, self.every = describe, every
        self.stream = stream if stream is not None else sys.stdout
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, name="heartbeat", daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def _beat(self) -> None:
        while not self._stop.wait(self.every):
            try:
                line = self.describe()
            except Exception as error:  # noqa: BLE001 - a broken line must not stop the beat
                line = f"(heartbeat could not describe the job: {error!r})"
            print(f"{time.strftime('%H:%M:%S')} {line}", file=self.stream, flush=True)


def _clock(seconds: float) -> str:
    """A duration a person can read at a glance."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def peak_memory_gb() -> float:
    """The most memory this process has held so far, in GB.

    For a job's progress lines: memory a run needs is easier to plan for when
    each stage reports the high-water mark it reached.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes.
    return peak / 1e9 if sys.platform == "darwin" else peak / 1e6
