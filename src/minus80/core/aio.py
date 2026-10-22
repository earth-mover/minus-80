"""Running a coroutine from synchronous code."""

from __future__ import annotations

from collections.abc import Coroutine
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
