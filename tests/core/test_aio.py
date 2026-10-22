import asyncio

import pytest

from minus80.core.aio import run_sync


async def _answer():
    await asyncio.sleep(0)
    return 42


def test_runs_a_coroutine_from_plain_code():
    assert run_sync(_answer()) == 42


def test_runs_a_coroutine_inside_a_running_loop():
    async def caller():
        coro = _answer()
        with pytest.raises(RuntimeError):
            asyncio.run(coro)
        coro.close()
        return run_sync(_answer())

    assert asyncio.run(caller()) == 42
