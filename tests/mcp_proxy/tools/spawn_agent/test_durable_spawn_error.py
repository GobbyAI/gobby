"""Originating launch errors survive capture failure and either cancellation path."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.agents.spawn_in_doubt_owner import release_claim
from gobby.mcp_proxy.tools.spawn_agent import _implementation
from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import cleanup_failed_spawn
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.terminals.in_doubt import in_doubt_spawns

pytestmark = pytest.mark.integration

HELD_TERMINAL = "31000000-0000-4000-8000-000000000001"
HELD_ORIGIN = "lifecycle publication failed: terminal proxy relay closed: ws_close"


def _held_spawn(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A run holding its task's dispatch mutex, bound to a terminal the in-doubt owner holds."""
    machine_id = "21000000-0000-4000-8000-000000000002"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="held-publication-retry",
        machine_id=machine_id,
        source="codex",
        project_id=PERSONAL_PROJECT_ID,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=PERSONAL_PROJECT_ID,
        title="Held publication retry",
        validation_criteria="A held terminal releases the mutex only after its owner settles.",
    )
    storage = LocalAgentRunManager(temp_db)
    run = storage.create(
        parent_session_id=session.id, provider="codex", prompt="test launch", task_id=task.id
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id, holder="first-spawn", kind="spawn_agent", ttl_seconds=300, run_id=run.id
    )
    runtime = Mock()
    runtime.terminate = AsyncMock()
    runtime_registry = Mock()
    runtime_registry.resolve.return_value = runtime
    terminal = SimpleNamespace(
        id=HELD_TERMINAL,
        backend="native",
        state="pending",
        spawn_key="held-publication-retry",
        attempt_generation=0,
        attempt_started_at=datetime(2026, 10, 28, tzinfo=UTC),
    )
    terminal_manager = Mock()
    terminal_manager.get.return_value = terminal
    runner = SimpleNamespace(
        run_storage=storage,
        session_storage=sessions,
        cancel_run=storage.cancel,
        get_run=storage.get,
        agent_lifecycle_monitor=None,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=runtime_registry,
    )
    return SimpleNamespace(
        runner=runner, run=run, task=task, tasks=tasks, mutexes=mutexes, runtime=runtime
    )


async def _fail_held_spawn(spawn: SimpleNamespace) -> None:
    await cleanup_failed_spawn(
        spawn.runner,
        spawn.run.id,
        HELD_ORIGIN,
        None,
        None,
        completion_registry=None,
        cleanup_isolation=False,
        task_manager=spawn.tasks,
        terminal_id=HELD_TERMINAL,
    )


async def test_held_terminal_keeps_mutex_until_owner_settles_then_respawn_is_immediate(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _held_spawn(temp_db, monkeypatch)
    assert in_doubt_spawns.claim(HELD_TERMINAL)
    try:
        await _fail_held_spawn(spawn)
        # A held id is never killed by the spawn's cleanup, so the process may still run.
        spawn.runtime.terminate.assert_not_awaited()
        assert spawn.mutexes.get_mutex(spawn.task.id) is not None
        await release_claim(HELD_TERMINAL, proven=True)
        assert spawn.mutexes.get_mutex(spawn.task.id) is None
        assert spawn.mutexes.acquire_mutex(
            spawn.task.id, holder="immediate-respawn", kind="spawn_agent", ttl_seconds=300
        )
    finally:
        in_doubt_spawns.release(HELD_TERMINAL)


async def test_held_terminal_orphan_settlement_releases_mutex_after_owner_decides(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _held_spawn(temp_db, monkeypatch)
    assert in_doubt_spawns.claim(HELD_TERMINAL)
    try:
        await _fail_held_spawn(spawn)
        spawn.runtime.terminate.assert_not_awaited()
        assert spawn.mutexes.get_mutex(spawn.task.id) is not None
        await release_claim(HELD_TERMINAL, proven=False)
        recorded = spawn.runner.run_storage.get(spawn.run.id)
        assert recorded is not None and recorded.status == "cancelled"
        assert spawn.mutexes.get_mutex(spawn.task.id) is None
    finally:
        in_doubt_spawns.release(HELD_TERMINAL)


async def test_lifecycle_publication_failure_releases_mutex_for_immediate_respawn(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="publication-mutex-cleanup",
        machine_id=machine_id,
        source="codex",
        project_id=PERSONAL_PROJECT_ID,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=PERSONAL_PROJECT_ID,
        title="Publication retry",
        validation_criteria="Failed publication permits immediate respawn.",
    )
    storage = LocalAgentRunManager(temp_db)
    run = storage.create(
        parent_session_id=session.id, provider="codex", prompt="test launch", task_id=task.id
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id, holder="first-spawn", kind="spawn_agent", ttl_seconds=300, run_id=run.id
    )
    runner = SimpleNamespace(
        run_storage=storage,
        session_storage=sessions,
        cancel_run=storage.cancel,
        get_run=storage.get,
        agent_lifecycle_monitor=None,
        terminal_manager=None,
        terminal_runtime_registry=None,
    )
    origin = "lifecycle publication failed: terminal proxy relay closed: ws_close"
    await cleanup_failed_spawn(
        runner,
        run.id,
        origin,
        None,
        None,
        completion_registry=None,
        cleanup_isolation=False,
        task_manager=tasks,
    )
    recorded = storage.get(run.id)
    assert recorded is not None and recorded.status == "cancelled"
    assert recorded.error == origin
    assert mutexes.get_mutex(task.id) is None
    assert mutexes.acquire_mutex(
        task.id, holder="immediate-respawn", kind="spawn_agent", ttl_seconds=300
    )


@pytest.mark.parametrize("with_monitor", [True, False])
async def test_originating_error_is_durable_before_capture_and_terminalization(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, with_monitor: bool
) -> None:
    machine_id = "21000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="durable-spawn-error",
        machine_id=machine_id,
        source="codex",
        project_id=PERSONAL_PROJECT_ID,
    )
    storage = LocalAgentRunManager(temp_db)
    run = storage.create(parent_session_id=session.id, provider="codex", prompt="test launch")
    origin = "tmux launch rejected 40960-byte command: message too long"

    async def capture(_row: object) -> SimpleNamespace:
        recorded = storage.get(run.id)
        assert recorded is not None and recorded.error == origin
        raise OSError("capture unavailable after launch failure")

    async def terminalize(run_id: str, *, terminal_reason: str) -> bool:
        recorded = storage.get(run_id)
        assert recorded is not None and recorded.error == origin
        assert terminal_reason == "spawn_rollback"
        return storage.cancel(run_id, terminal_reason="spawn_rollback") is not None

    terminal = SimpleNamespace(
        id="terminal-1",
        backend="native",
        state="pending",
        spawn_key="isolated-spawn-test",
        attempt_generation=0,
        attempt_started_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    runtime = Mock()
    runtime.is_live = AsyncMock(return_value=True)
    runtime.snapshot_full = AsyncMock(side_effect=capture)
    runtime.terminate = AsyncMock()
    runtime.session_present = AsyncMock(return_value=False)
    terminal_runtime_registry = Mock()
    terminal_runtime_registry.resolve.return_value = runtime
    terminal_manager = Mock()
    terminal_manager.get.return_value = terminal
    monitor = (
        SimpleNamespace(terminalize_cancelled_run=AsyncMock(side_effect=terminalize))
        if with_monitor
        else None
    )
    runner = SimpleNamespace(
        run_storage=storage,
        session_storage=sessions,
        cancel_run=storage.cancel,
        get_run=storage.get,
        agent_lifecycle_monitor=monitor,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=terminal_runtime_registry,
    )
    await cleanup_failed_spawn(
        runner,
        run.id,
        origin,
        None,
        None,
        completion_registry=None,
        cleanup_isolation=False,
        task_manager=None,
        terminal_id=terminal.id,
    )
    recorded = storage.get(run.id)
    assert recorded is not None and recorded.status == "cancelled"
    assert recorded.error == origin
    runtime.terminate.assert_awaited()
    terminal_manager.fail_pending_attempt.assert_called_once_with(
        terminal.id,
        attempt_generation=terminal.attempt_generation,
        attempt_started_at=terminal.attempt_started_at,
    )
    if monitor is not None:
        monitor.terminalize_cancelled_run.assert_awaited_once()


UNBOUND_FRONT_DOOR = {
    "success": False,
    "error": "native WS front door unbound; spawn refused until the publisher rebinds",
}


def _front_door(generation: str | None) -> SimpleNamespace:
    publisher = SimpleNamespace(generation=generation)
    return SimpleNamespace(websocket_server=SimpleNamespace(native_publisher=publisher))


async def test_spawn_is_refused_while_the_native_front_door_is_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_implementation, "get_app_context", lambda: _front_door(None))
    runner = Mock()
    result = await _implementation.spawn_agent_impl("launch while the front door is down", runner)
    assert result == UNBOUND_FRONT_DOOR
    runner.run_storage.create.assert_not_called()


def test_front_door_refusal_clears_once_the_publisher_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_implementation, "get_app_context", lambda: _front_door(None))
    assert _implementation._native_front_door_refusal() == UNBOUND_FRONT_DOOR
    monkeypatch.setattr(_implementation, "get_app_context", lambda: _front_door("gen-1"))
    assert _implementation._native_front_door_refusal() is None
