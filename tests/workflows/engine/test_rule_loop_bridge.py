"""Tests for rule-loop bridge timing."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from gobby.workflows.engine.evaluation import _RuleLoopBridge

pytestmark = pytest.mark.unit


@pytest.fixture
def daemon_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


async def _await_timer(delay: float) -> None:
    released = asyncio.Event()
    asyncio.get_running_loop().call_later(delay, released.set)
    await released.wait()


@pytest.mark.asyncio
async def test_bridge_returns_result_once_daemon_loop_frees(
    daemon_loop: asyncio.AbstractEventLoop,
) -> None:
    gate = threading.Event()
    # Occupy the daemon loop until the worker releases it 50 ms later.
    daemon_loop.call_soon_threadsafe(gate.wait, 5.0)
    asyncio.get_running_loop().call_later(0.05, gate.set)

    async def bridged() -> str:
        await _await_timer(0.01)
        return "done"

    result = await _RuleLoopBridge(daemon_loop).call(bridged)

    assert result == "done"
    assert gate.is_set()


@pytest.mark.asyncio
async def test_bridge_propagates_bridged_call_error(
    daemon_loop: asyncio.AbstractEventLoop,
) -> None:
    async def failing() -> None:
        await _await_timer(0.01)
        raise RuntimeError("dispatch failed")

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await _RuleLoopBridge(daemon_loop).call(failing)
