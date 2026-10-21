"""Saying what a long job is doing, often enough to tell working from hung.

An ingest can spend minutes inside one call — walking a directory chain,
fetching a metadata region, asking an archive's API about every image — and a
process that prints nothing for three minutes is indistinguishable from one
that has stalled. The usual reflex is to shorten a timeout, which only turns an
unreadable wait into an unexplained failure.

So report on a **time interval, not a count**. A count threshold cannot know
how long its work takes: 100,000 pages is thirty seconds of silence for one
file and forty minutes for another. An interval reports the slow thing and
stays quiet about the fast one, which is what makes the output worth reading.

Two rules the callers here follow:

- **Say what it is before saying how far.** "walking 829,080 directories" first,
  then the fractions. A fraction alone does not say what is being counted.
- **Give a denominator wherever one is known.** "300,000 of 829,080, 36%, about
  40 s left" tells you whether to wait; "300,000 so far" does not.
"""

from __future__ import annotations

import sys
import time
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
        # A closing line only where one was promised: a job that finished
        # before the first interval said nothing and needs no epilogue.
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


def _clock(seconds: float) -> str:
    """A duration a person can read at a glance."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"
