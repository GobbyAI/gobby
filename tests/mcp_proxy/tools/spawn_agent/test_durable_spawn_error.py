"""Originating launch errors survive capture failure and either cancellation path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import cleanup_failed_spawn
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration


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
    )
    runtime = Mock()
    runtime.is_live = AsyncMock(return_value=True)
    runtime.snapshot_full = AsyncMock(side_effect=capture)
    runtime.terminate = AsyncMock()
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
    terminal_manager.fail_pending.assert_called_once_with(terminal.id)
    if monitor is not None:
        monitor.terminalize_cancelled_run.assert_awaited_once()
