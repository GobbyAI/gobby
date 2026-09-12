"""Event-loop responsiveness coverage for agent spawn preparation."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.mcp_proxy.tools.spawn_agent import _implementation
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_spawn_preparation_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A three-second synchronous prepare leaves sub-500ms heartbeat gaps."""
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner._child_session_manager = MagicMock()
    runner.run_storage = MagicMock()

    handler = MagicMock()
    handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/path"))
    handler.build_context_prompt.return_value = "test"

    def blocking_prepare(*_args: object, **_kwargs: object) -> object:
        assert threading.Event().wait(timeout=3) is False
        return prepared_spawn()

    spawn_result = MagicMock(
        success=True,
        child_session_id="child",
        status="ok",
        pid=1,
        backend=None,
        terminal_id=None,
        message="ok",
        process=None,
    )
    monkeypatch.setattr(_implementation, "prepare_terminal_spawn", blocking_prepare)
    monkeypatch.setattr(
        _implementation,
        "get_project_context",
        lambda _path: {
            "id": "11111111-1111-4111-8111-111111110001",
            "project_path": "/path",
        },
    )
    monkeypatch.setattr(_implementation, "get_isolation_handler", lambda *_args, **_kwargs: handler)
    execute_spawn = AsyncMock(return_value=spawn_result)
    monkeypatch.setattr(_implementation, "execute_spawn", execute_spawn)

    loop = asyncio.get_running_loop()
    heartbeat_times = [loop.time()]
    stop_heartbeat = asyncio.Event()
    heartbeat_started = asyncio.Event()

    async def heartbeat() -> None:
        heartbeat_started.set()
        while not stop_heartbeat.is_set():
            tick = loop.create_future()
            handle = loop.call_later(0.02, tick.set_result, None)
            try:
                await tick
            finally:
                handle.cancel()
            heartbeat_times.append(loop.time())

    heartbeat_task = asyncio.create_task(heartbeat())
    await heartbeat_started.wait()
    try:
        result = await _implementation.spawn_agent_impl(
            terminal_backend="tmux",
            prompt="test",
            runner=runner,
            provider="claude",
            parent_session_id="parent",
        )
        heartbeat_times.append(loop.time())
    finally:
        stop_heartbeat.set()
        await heartbeat_task
        background_tasks = tuple(_implementation._spawn_background_tasks.values())
        if background_tasks:
            await asyncio.gather(*background_tasks)

    gaps = [
        later - earlier
        for earlier, later in zip(heartbeat_times, heartbeat_times[1:], strict=False)
    ]
    assert result["success"] is True
    assert max(gaps) < 0.5
    assert execute_spawn.await_args is not None
    spawn_request = execute_spawn.await_args.args[0]
    assert spawn_request.phase_timings_ms["prepare_terminal_spawn"] >= 2900
