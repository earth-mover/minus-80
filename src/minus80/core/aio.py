"""Running coroutines from synchronous code."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

T = TypeVar("T")


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion from synchronous code, inside a running event loop or not.

    ``asyncio.run`` raises inside a running loop, which is where a Jupyter cell
    runs. zarr's ``sync`` runs the coroutine on zarr's own IO-loop thread, so
    no caller's loop is re-entered; zarr, VirtualiZarr and icechunk already
    depend on it.
    """
    from zarr.core.sync import sync

    return sync(coro)


def submit(coro: Coroutine[Any, Any, T], loop: asyncio.AbstractEventLoop | None = None) -> Future[T]:
    """Start ``coro`` on ``loop``, by default the one :func:`run_sync` uses, and return a future for it.

    Cancelling the future cancels the coroutine, unlike a thread-pool future,
    which cannot be cancelled once it starts. The loop runs on a daemon thread,
    so work left on it does not hold the interpreter open at exit.
    """
    if loop is None:
        from zarr.core.sync import _get_loop

        loop = _get_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)


_loops: dict[str, asyncio.AbstractEventLoop] = {}
_loops_lock = threading.Lock()


def own_loop(name: str) -> asyncio.AbstractEventLoop:
    """An event loop running on its own daemon thread, one per ``name``, started on first use.

    zarr runs every store call of every array on one loop, and a store method
    that blocks instead of awaiting stalls everything on that loop. Requests
    submitted to a loop of their own keep going while zarr's loop is held.
    """
    with _loops_lock:
        if name not in _loops:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name=f"{name}-loop", daemon=True).start()
            _loops[name] = loop
        return _loops[name]
