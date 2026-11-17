from __future__ import annotations

import asyncio

import pytest

from gobby.storage.hub.async_ops import _RunState, _terminate_child


@pytest.mark.unit
@pytest.mark.asyncio
async def test_expired_cleanup_delivers_cancellation_before_declaring_child_unresponsive() -> None:
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def cooperative_child() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    child = asyncio.create_task(cooperative_child())
    await started.wait()
    try:
        error = await _terminate_child(
            child,
            _RunState(),
            cleanup_deadline=asyncio.get_running_loop().time() - 1.0,
        )
        assert isinstance(error, asyncio.CancelledError)
        assert child.done()
        assert finalized.is_set()
    finally:
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
