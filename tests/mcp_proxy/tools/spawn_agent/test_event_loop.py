"""Event-loop responsiveness coverage for agent spawn preparation."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.mcp_proxy.tools.spawn_agent import _implementation
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import TaskSpawnLease
from tests.agents.prepared_spawn import prepared_spawn

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]


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


@pytest.mark.asyncio
async def test_cancelled_prepare_waits_for_worker_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner._child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    handler = MagicMock()
    handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/path"))
    handler.build_context_prompt.return_value = "test"
    entered = threading.Event()
    release = threading.Event()
    committed = threading.Event()
    lease_released = threading.Event()

    def blocking_prepare(*_args: object, **_kwargs: object) -> object:
        entered.set()
        assert release.wait(timeout=5)
        committed.set()
        return prepared_spawn()

    monkeypatch.setattr(_implementation, "prepare_terminal_spawn", blocking_prepare)
    monkeypatch.setattr(
        _implementation,
        "get_project_context",
        lambda _path: {"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
    )
    monkeypatch.setattr(_implementation, "get_isolation_handler", lambda *_a, **_kw: handler)
    monkeypatch.setattr(
        TaskSpawnLease,
        "release_unattached",
        lambda _self: lease_released.set(),
    )

    async def record_cleanup(*_args: object, **_kwargs: object) -> None:
        assert committed.is_set()
        assert lease_released.is_set()

    cleanup = AsyncMock(side_effect=record_cleanup)
    execute_spawn = AsyncMock()
    monkeypatch.setattr(_implementation, "cleanup_failed_spawn", cleanup)
    monkeypatch.setattr(_implementation, "execute_spawn", execute_spawn)
    spawn = asyncio.create_task(
        _implementation.spawn_agent_impl(
            terminal_backend="tmux",
            prompt="test",
            runner=runner,
            provider="claude",
            parent_session_id="parent",
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        spawn.cancel()
        await asyncio.sleep(0)
        assert not spawn.done()
        assert not committed.is_set()
        spawn.cancel()
        await asyncio.sleep(0)
        assert not spawn.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await spawn
    assert committed.is_set()
    assert lease_released.is_set()
    cleanup.assert_awaited_once()
    assert cleanup.await_args is not None
    assert cleanup.await_args.args[2] == "Agent spawn cancelled during preparation"
    execute_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_lease_acquire_waits_then_releases_unattached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner.run_storage = MagicMock()
    handler = MagicMock()
    handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/path"))
    handler.build_context_prompt.return_value = "test"
    entered = threading.Event()
    release = threading.Event()
    acquired = threading.Event()
    lease_released = threading.Event()

    def acquire(_self: TaskSpawnLease) -> None:
        entered.set()
        assert release.wait(timeout=5)
        acquired.set()

    def release_lease(_self: TaskSpawnLease) -> None:
        assert acquired.is_set()
        lease_released.set()

    monkeypatch.setattr(TaskSpawnLease, "acquire", acquire)
    monkeypatch.setattr(TaskSpawnLease, "release_unattached", release_lease)
    monkeypatch.setattr(
        _implementation,
        "get_project_context",
        lambda _path: {"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
    )
    monkeypatch.setattr(_implementation, "get_isolation_handler", lambda *_a, **_kw: handler)
    cleanup = AsyncMock()
    monkeypatch.setattr(_implementation, "cleanup_created_isolation", cleanup)
    prepare = MagicMock()
    monkeypatch.setattr(_implementation, "prepare_terminal_spawn", prepare)

    spawn = asyncio.create_task(
        _implementation.spawn_agent_impl(
            terminal_backend="tmux",
            prompt="test",
            runner=runner,
            provider="claude",
            parent_session_id="parent",
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        spawn.cancel()
        await asyncio.sleep(0)
        assert not spawn.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await spawn
    assert lease_released.is_set()
    cleanup.assert_awaited_once()
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_slot_entry_releases_acquired_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner.run_storage = MagicMock()
    handler = MagicMock()
    handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/path"))
    handler.build_context_prompt.return_value = "test"
    acquired = False
    released = False
    entered = asyncio.Event()

    def acquire(_self: TaskSpawnLease) -> None:
        nonlocal acquired
        acquired = True

    def release_lease(_self: TaskSpawnLease) -> None:
        nonlocal released
        released = True

    @asynccontextmanager
    async def blocked_slot(**kwargs: Any) -> AsyncIterator[None]:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await kwargs["on_entry_cancel"]()
            raise
        yield None

    monkeypatch.setattr(TaskSpawnLease, "acquire", acquire)
    monkeypatch.setattr(TaskSpawnLease, "release_unattached", release_lease)
    monkeypatch.setattr(_implementation, "reserve_agent_slot", blocked_slot)
    monkeypatch.setattr(
        _implementation,
        "get_project_context",
        lambda _path: {"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
    )
    monkeypatch.setattr(_implementation, "get_isolation_handler", lambda *_a, **_kw: handler)
    cleanup = AsyncMock()
    monkeypatch.setattr(_implementation, "cleanup_created_isolation", cleanup)
    prepare = MagicMock()
    monkeypatch.setattr(_implementation, "prepare_terminal_spawn", prepare)

    spawn = asyncio.create_task(
        _implementation.spawn_agent_impl(
            terminal_backend="tmux",
            prompt="test",
            runner=runner,
            provider="claude",
            parent_session_id="parent",
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    spawn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await spawn
    assert acquired and released
    cleanup.assert_awaited_once()
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_post_prepare_attach_waits_then_rolls_back_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "ok", 0)
    runner._child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    handler = MagicMock()
    handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/path"))
    handler.build_context_prompt.return_value = "test"
    entered = threading.Event()
    release = threading.Event()
    attached = threading.Event()
    lease_released = threading.Event()

    def blocking_attach(_self: TaskSpawnLease, _run_id: str) -> None:
        entered.set()
        assert release.wait(timeout=5)
        attached.set()

    monkeypatch.setattr(TaskSpawnLease, "attach", blocking_attach)
    monkeypatch.setattr(TaskSpawnLease, "release_unattached", lambda _self: lease_released.set())
    monkeypatch.setattr(_implementation, "prepare_terminal_spawn", lambda **_kw: prepared_spawn())
    monkeypatch.setattr(
        _implementation,
        "get_project_context",
        lambda _path: {"id": "11111111-1111-4111-8111-111111110001", "project_path": "/path"},
    )
    monkeypatch.setattr(_implementation, "get_isolation_handler", lambda *_a, **_kw: handler)

    async def record_cleanup(*_args: object, **_kwargs: object) -> None:
        assert attached.is_set()
        assert lease_released.is_set()

    cleanup = AsyncMock(side_effect=record_cleanup)
    monkeypatch.setattr(_implementation, "cleanup_failed_spawn", cleanup)
    execute_spawn = AsyncMock()
    monkeypatch.setattr(_implementation, "execute_spawn", execute_spawn)
    spawn = asyncio.create_task(
        _implementation.spawn_agent_impl(
            terminal_backend="tmux",
            prompt="test",
            runner=runner,
            provider="claude",
            parent_session_id="parent",
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        spawn.cancel()
        await asyncio.sleep(0)
        assert not spawn.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await spawn
    cleanup.assert_awaited_once()
    assert cleanup.await_args is not None
    assert cleanup.await_args.args[2] == "Agent spawn cancelled before launch"
    assert cleanup.await_args.kwargs["child_session_id"] == prepared_spawn().session_id
    execute_spawn.assert_not_awaited()
