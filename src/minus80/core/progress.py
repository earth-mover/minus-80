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

:class:`StallWatch` covers what neither can: a job that is alive but no longer
moving. When its counters stop changing, it prints the stack of every thread,
so the log shows where each one is waiting even when nothing can attach to the
process from outside.

Callers follow two rules:

- **Say what it is before saying how far.** "walking 829,080 directories" first,
  then the fractions. A fraction alone does not say what is being counted.
- **Give a denominator wherever one is known.** "300,000 of 829,080, 36%, about
  40 s left" tells you whether to wait; "300,000 so far" does not.
"""

from __future__ import annotations

import faulthandler
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


class StallWatch:
    """Print every thread's stack when ``signature()`` has not changed for ``after`` seconds.

    ``signature`` returns the job's counters, such as bytes fetched and items
    written; a job that is moving changes at least one of them. While the
    stall lasts the stacks are printed again every ``again`` seconds, and a
    line says when it ends. With ``log``, the path of a file another part of
    the job writes (such as a library's debug log), its last ``tail`` lines
    are printed with each dump.

    The stacks are Python's only: a thread waiting inside a compiled library
    shows the Python call that entered it.
    """

    def __init__(self, signature: Callable[[], object], *, after: float = 60.0, again: float = 300.0,
                 log: str | None = None, tail: int = 40, stream=None):
        self.signature, self.after, self.again = signature, after, again
        self.log, self.tail = log, tail
        self.stream = stream if stream is not None else sys.stdout
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="stall-watch", daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def _watch(self) -> None:
        last, since, dumped = self.signature(), time.monotonic(), None
        while not self._stop.wait(min(5.0, self.after)):
            current, now = self.signature(), time.monotonic()
            if current != last:
                if dumped is not None:
                    self._say(f"stall ended after {now - since:.0f} s")
                last, since, dumped = current, now, None
                continue
            if now - since >= self.after and (dumped is None or now - dumped >= self.again):
                self._dump(now - since)
                dumped = now

    def _dump(self, stalled: float) -> None:
        self._say(f"no progress for {stalled:.0f} s; every thread's stack follows")
        self.stream.flush()
        try:
            faulthandler.dump_traceback(file=self.stream, all_threads=True)
        except (OSError, ValueError, AttributeError) as error:
            self._say(f"(could not dump the stacks: {error!r})")
        if self.log:
            try:
                with open(self.log, errors="replace") as f:
                    lines = f.readlines()[-self.tail:]
                self._say(f"last {len(lines)} lines of {self.log}:")
                for line in lines:
                    print(f"    {line.rstrip()}", file=self.stream)
            except OSError as error:
                self._say(f"(could not read {self.log}: {error!r})")
        self.stream.flush()

    def _say(self, line: str) -> None:
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
