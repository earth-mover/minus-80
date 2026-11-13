"""The plumbing that runs the zebrafish copy's stages at once.

``ingest.py`` holds the stages themselves and the parsing of the source: what
each channel run is, how it is decoded and where it is written. This module
holds what lets those stages run side by side and report on themselves:

- the stage threads, and the queue each one reports its failure on;
- the byte budgets that hold a fast stage back when a slower one falls behind;
- the counters and timings behind the progress lines;
- the repo, the writable session a well is copied in, and the source reads
  that sit outside the stream.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import zarr
from zarr.core.buffer import default_buffer_prototype
from zarr.storage import MemoryStore

from minus80.core.fetch import read_range
from minus80.core.icechunk_io import ARRAYLAKE, arraylake_repository, commit, storage_for
from minus80.core.metered import Meter, MeteredStore
from minus80.core.progress import Progress, peak_memory_gb
from minus80.parsers.zip.directory import member_stored_bytes
from minus80.parsers.zip.streaming import StoredMember


def say(line: str) -> None:
    """Print one progress line at once, with the time, so a remote run's log shows where it is."""
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


def open_repository(location: str):
    """Open an Icechunk repo at a local path, creating it if needed, or open an Arraylake repo."""
    import icechunk

    if location.startswith(ARRAYLAKE):
        return arraylake_repository(location)
    return icechunk.Repository.open_or_create(storage_for(location))


def documents_store(documents: Mapping[str, bytes], path: str = "") -> zarr.Group:
    """A read-only zarr group over metadata documents held in memory, opened at ``path``.

    Opening a group at its path reads only that group's own documents, so it
    works before the documents of the groups above it have arrived.
    """
    prototype = default_buffer_prototype()
    store = MemoryStore({name: prototype.buffer.from_bytes(data) for name, data in documents.items()})
    return zarr.open_group(store, path=path, mode="r", zarr_format=2)


def read_member_again(url: str, member: StoredMember, registry=None) -> bytes:
    """The member's stored bytes, fetched again on their own, from its local header to the next member."""
    entry = member.entry
    span = read_range(url, entry["start"], entry["end"], patient=True, registry=registry)
    return member_stored_bytes(span, entry["start"], entry)


class Budget:
    """The bytes one stage of the copy may hold at once.

    Why it is needed:

    - Each stage runs on its own thread at its own pace. Without a limit, a
      fast stage would pile up bytes ahead of a slower one.

    What it does:

    - ``take`` waits while the bytes held would pass the limit. A stage
      holding nothing may always take, so one item larger than the limit
      still goes through.
    - ``give`` hands bytes back and wakes a stage waiting to take.
    - ``waited`` adds up the seconds spent waiting. They show how long a
      later stage held this one back.
    """

    def __init__(self, limit: int, stop: threading.Event) -> None:
        self.limit, self.stop = limit, stop
        self.held = 0
        self.waited = 0.0
        self.changed = threading.Condition()

    def take(self, n: int) -> None:
        with self.changed:
            started = time.monotonic()
            while self.held and self.held + n > self.limit:
                if self.stop.is_set():
                    raise RuntimeError("stopped")
                self.changed.wait(1)
            self.waited += time.monotonic() - started
            self.held += n

    def give(self, n: int) -> None:
        with self.changed:
            self.held -= n
            self.changed.notify_all()


#: Seconds to wait for each of the copy's threads to stop once the copy has
#: ended or failed. They are daemon threads, so one stuck in a request does
#: not keep the process alive, and the copy's own error surfaces without
#: waiting on it.
THREAD_JOIN_TIMEOUT = 10.0


def start_stage(name: str, out: queue.Queue, work: Callable, *args, **kwargs) -> threading.Thread:
    """Start ``work`` on a daemon thread of its own, passing any failure to ``out`` as ``("error", error)``.

    The thread that reads ``out`` raises the error and stops the well, so a
    stage that fails never leaves the others waiting on it.
    """

    def run() -> None:
        try:
            work(*args, **kwargs)
        except BaseException as error:  # noqa: BLE001 - every failure must reach the waiting thread
            out.put(("error", error))

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


def next_item(items: queue.Queue, stop: threading.Event):
    """The next item on ``items``, waiting for it until ``stop`` is set."""
    while True:
        try:
            return items.get(timeout=1)
        except queue.Empty:
            if stop.is_set():
                raise RuntimeError("stopped") from None


class WellSession:
    """The writable session a well is copied in, renewed at each checkpoint.

    A commit leaves an Icechunk session read-only, so each checkpoint commits
    and opens a new writable session on the branch. zarr writes through a
    :class:`~minus80.core.metered.MeteredStore`, which adds every shard
    written to ``meter``.
    """

    def __init__(self, repo, branch: str, meter: Meter) -> None:
        self.repo, self.branch, self.meter = repo, branch, meter
        self.open()

    def open(self) -> None:
        self.session = self.repo.writable_session(self.branch)
        store = MeteredStore(self.session.store, self.meter, name=f"icechunk://{self.branch}")
        self.dest = zarr.open_group(store, mode="a", zarr_format=3)

    def commit(self, message: str) -> None:
        commit(self.session, message)
        self.open()


class Counters:
    """What one well's copy has done so far, for the heartbeat line.

    The threads of the copy update these as they work, and the heartbeat
    reads them from its own thread.
    """

    def __init__(self, well: str) -> None:
        self.well = well
        #: The stage the well is in, named in the heartbeat line.
        self.stage = "streaming"
        self.started = time.monotonic()
        #: Bytes of the well's region fetched and cut into members.
        self.fetched = 0
        self.decoding: Progress | None = None
        self.uploaded = 0
        #: Compressed bytes of the channel runs waiting for metadata.
        self.waiting = 0
        #: The copy's budgets, once it starts, for what each holds.
        self.fetched_held: Budget | None = None
        self.decoded_held: Budget | None = None
        #: The shards written to the store: their stored bytes and time.
        self.meter: Meter | None = None
        self.lock = threading.Lock()
        #: The clock and the CPU seconds used at the last line.
        self.cpu_mark = (time.monotonic(), sum(os.times()[:2]))

    def uploaded_field(self) -> None:
        with self.lock:
            self.uploaded += 1

    def line(self) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        decoded = self.decoding.done if self.decoding is not None else 0
        held = stored = ""
        if self.fetched_held is not None and self.decoded_held is not None:
            held = (
                f"; holding {self.fetched_held.held / 1e9:.1f} GB fetched and not yet decoded, "
                f"{self.decoded_held.held / 1e9:.1f} GB being decoded or waiting to upload"
            )
        if (m := self.meter) is not None:
            mean = m.seconds / m.writes if m.writes else 0.0
            stored = (
                f"; stored {m.bytes / 1e9:.2f} GB ({m.bytes / elapsed / 1e6:.0f} MB/s) in {m.writes:,} shard writes, "
                f"{m.running} running, {mean:.1f} s each on average, slowest {m.slowest:.0f} s"
            )
        # CPU seconds used per second since the last line: how many cores the copy kept busy.
        now, cpu = time.monotonic(), sum(os.times()[:2])
        (then, before), self.cpu_mark = self.cpu_mark, (now, cpu)
        cores_busy = (cpu - before) / max(now - then, 1e-9)
        return (
            f"well {self.well}, {self.stage}: fetched {self.fetched / 1e9:.2f} GB "
            f"({self.fetched / elapsed / 1e6:.0f} MB/s), {decoded:,} chunks decoded, "
            f"{self.uploaded:,} fields uploaded, "
            f"{self.waiting / 1e9:.2f} GB waiting for metadata{held}{stored}; "
            f"{cores_busy:.1f} cores busy, peak memory {peak_memory_gb():.1f} GB"
        )

    def signature(self) -> tuple:
        """What changes while the copy moves, for :class:`~minus80.core.progress.StallWatch`."""
        decoded = self.decoding.done if self.decoding is not None else 0
        writes = self.meter.writes if self.meter is not None else 0
        return (self.stage, self.fetched, decoded, self.uploaded, writes)


@dataclass
class Timings:
    """Seconds each stage of a well took, for the progress lines.

    The stages run at once, so these do not add up to the well's time. The
    ``held back`` figures say which stage set the pace: the fetch waits when
    decoding falls behind, and decoding waits when uploads fall behind.
    """

    decode: float = 0.0
    fetch_held_back: float = 0.0
    decode_held_back: float = 0.0
    checkpoints: int = 0
    checkpoint_seconds: float = 0.0
    commit: float = 0.0

    def line(self, total: float) -> str:
        return (
            f"{total:.0f} s in all: decoding {self.decode:.0f} s; fetch held back by decoding "
            f"{self.fetch_held_back:.0f} s, decoding held back by uploads {self.decode_held_back:.0f} s; "
            f"{self.checkpoints} checkpoints, {self.checkpoint_seconds:.0f} s; final commit {self.commit:.0f} s"
        )
