"""Write an OME-Zarr element in units, one commit per unit, and mark the element complete.

A run that stops between units keeps what it committed.
A rerun asks :meth:`ElementWriter.has` which units the branch holds, and writes the rest.
Then :meth:`ElementWriter.finish` sets :data:`COMPLETE` on the element's group.
So a reader tells a whole element from a part.
"""

from __future__ import annotations

import time
from collections.abc import Collection
from contextlib import contextmanager

import zarr

from .icechunk_io import commit
from .progress import MIB, Progress
from .remote import ReadCounter

#: The group attribute that is true once the writer committed every unit of the element.
COMPLETE = "minus80:complete"


class ElementWriter:
    """One writable session and one commit per unit, with bytes read and wall time printed.

    ``counter`` is the :class:`~minus80.core.remote.ReadCounter` of the reader that feeds the
    units. ``protected`` lists the branches this writer refuses.
    """

    def __init__(self, repo, branch: str, *, counter: ReadCounter | None = None, protected: Collection[str] = ()):
        if branch in protected:
            raise ValueError(f"{branch} is a protected branch; this writer does not write to it")
        self.repo, self.branch, self.counter = repo, branch, counter

    def _root(self) -> zarr.Group | None:
        try:
            return zarr.open_group(self.repo.readonly_session(branch=self.branch).store, mode="r")
        except zarr.errors.GroupNotFoundError:  # A branch with no commit beyond the first has no root group.
            return None

    def has(self, path: str) -> bool:
        """True when the branch has a node at ``path``, so a rerun skips that unit.

        A unit counts as held only when its commit exists. A unit that raised has no commit.
        """
        root = self._root()
        return root is not None and path in root

    def has_attribute(self, key: str) -> bool:
        """True when the branch's root group has attribute ``key``, so a rerun skips that unit."""
        root = self._root()
        return root is not None and key in root.attrs

    def complete(self, path: str) -> bool:
        """True when the group at ``path`` is on the branch and :meth:`finish` marked it."""
        root = self._root()
        return root is not None and path in root and root[path].attrs.get(COMPLETE) is True

    @contextmanager
    def unit(self, path: str, message: str, *, total: int | None = None):
        """Yield a writable root and a progress line; commit with ``message`` only if the body raises nothing.

        A body that changes nothing makes no commit and prints one line.
        """
        session = self.repo.writable_session(self.branch)
        root = zarr.open_group(session.store, mode="a")
        start, before = time.monotonic(), self.counter.bytes if self.counter else 0
        with Progress(f"{path}: reading", total=total, unit="MiB", scale=MIB) as progress:
            if self.counter:
                self.counter.progress = progress
            try:
                yield root, progress
            finally:
                if self.counter:
                    self.counter.progress = None
        if not session.has_uncommitted_changes:
            # Icechunk refuses an empty commit; a rerun of a held unit changes nothing.
            print(f"{path}: on branch {self.branch} already, equal, skipped", flush=True)
            return
        snapshot = commit(session, message)
        read = f"{(self.counter.bytes - before) / MIB:,.1f} MiB read, " if self.counter else ""
        print(f"{path}: {read}{time.monotonic() - start:,.1f} s, commit {snapshot}", flush=True)

    def finish(self, path: str, message: str) -> str:
        """Mark the group at ``path`` complete and commit; return the snapshot."""
        session = self.repo.writable_session(self.branch)
        zarr.open_group(session.store, mode="a")[path].attrs[COMPLETE] = True
        return commit(session, message)
