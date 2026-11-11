"""A zarr store that counts what is written through it and how long each write takes.

Writing through zarr hides where the time goes: an assignment encodes its
shards and then hands each one to the store. Wrapping the store separates the
two. Each ``set`` is timed from the moment zarr hands the encoded bytes over
until the wrapped store returns, and its size is the stored size, after
compression, so a job can report the rate its output reaches storage.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from zarr.abc.store import Store
from zarr.core.buffer import Buffer
from zarr.storage import WrapperStore


@dataclass
class Meter:
    """Running totals of the writes through one or more :class:`MeteredStore`.

    Updated from zarr's event loop and read from any thread, under a lock.
    """

    writes: int = 0
    bytes: int = 0
    seconds: float = 0.0
    slowest: float = 0.0
    running: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def started(self) -> None:
        with self._lock:
            self.running += 1

    def finished(self, nbytes: int, seconds: float) -> None:
        with self._lock:
            self.running -= 1
            self.writes += 1
            self.bytes += nbytes
            self.seconds += seconds
            self.slowest = max(self.slowest, seconds)


class MeteredStore(WrapperStore[Store]):
    """Wraps a store, adding each ``set`` to ``meter``: its stored bytes and the seconds it took.

    A write that takes longer than ``slow`` seconds is reported on its own
    line. ``str()`` returns ``name`` without asking the wrapped store. zarr
    formats the store's name each time it opens or creates an array, on its
    event loop, and Icechunk's store describes itself with a blocking read of
    its session (``icechunk-python/src/store.rs``). That was suspected of
    stalling array creation during the S-BIAD3228 copy, but no stall ever
    reproduced; returning ``name`` avoids the read either way.
    """

    def __init__(self, store: Store, meter: Meter, *, name: str = "store", slow: float = 20.0) -> None:
        super().__init__(store)
        self.meter, self.name, self.slow = meter, name, slow

    def _with_store(self, store: Store) -> MeteredStore:
        return type(self)(store, self.meter, name=self.name, slow=self.slow)

    async def set(self, key: str, value: Buffer) -> None:
        started = time.monotonic()
        self.meter.started()
        try:
            await self._store.set(key, value)
        finally:
            took = time.monotonic() - started
            self.meter.finished(len(value), took)
            if took > self.slow:
                print(f"      slow write of {key} ({len(value) / 1e6:.1f} MB): {took:.0f} s", flush=True)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"MeteredStore({self.name!r})"
