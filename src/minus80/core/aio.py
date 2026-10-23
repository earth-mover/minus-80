"""Running coroutines from synchronous code."""

from __future__ import annotations

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


def submit(coro: Coroutine[Any, Any, T]) -> Future[T]:
    """Start ``coro`` on the loop :func:`run_sync` uses and return a future for it.

    Cancelling the future cancels the coroutine, unlike a thread-pool future,
    which cannot be cancelled once it starts. The loop runs on a daemon thread,
    so work left on it does not hold the interpreter open at exit.
    """
    import asyncio

    from zarr.core.sync import _get_loop

    return asyncio.run_coroutine_threadsafe(coro, _get_loop())
