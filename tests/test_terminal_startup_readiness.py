"""Terminal monitors leave startup recovery in charge until readiness is published."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gobby.agents.tmux.pane_monitor import TmuxPaneMonitor
from gobby.hooks.events import HookEventType
from gobby.sessions.liveness_monitor import SessionLivenessMonitor, _TerminalLivenessRecord
from gobby.terminal_ownership import OwnershipState
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminals.fakes import FakeRuntime, runtime_registry


async def test_missing_agent_terminal_waits_for_startup_reconciliation() -> None:
    ready = False
    callback = MagicMock()
    monitor = TmuxPaneMonitor(
        session_end_callback=callback,
        detection_registry=BundledDetectionRegistry(),
        session_manager=MagicMock(),
        registry=runtime_registry(FakeRuntime()),
        startup_ready=lambda: ready,
    )
    agent = SimpleNamespace(terminal_id="terminal", child_session_id="child", id="run")
    session = SimpleNamespace(id="child", external_id="external", source="codex")
    with (
        patch(
            "gobby.agents.tmux.pane_monitor.TmuxSessionManager.list_sessions",
            new_callable=AsyncMock,
            return_value=[],
        ) as inventory,
        patch.object(monitor, "_list_active_runs", new=AsyncMock(return_value=[agent])),
        patch.object(monitor, "_check_attention_panes", new=AsyncMock()),
        patch.object(monitor, "_lookup_session", return_value=session),
        patch(
            "gobby.storage.terminals.TerminalManager.get",
            return_value=SimpleNamespace(backend="tmux", session_name="missing"),
        ),
    ):
        await monitor._check_panes()
        inventory.assert_not_awaited()
        callback.assert_not_called()
        ready = True
        await monitor._check_panes()
    callback.assert_called_once()
    event = callback.call_args.args[0]
    assert event.event_type == HookEventType.SESSION_END
    assert event.metadata["_platform_session_id"] == "child"


async def test_session_liveness_waits_for_startup_reconciliation() -> None:
    ready = False
    monitor = SessionLivenessMonitor(session_storage=MagicMock(), startup_ready=lambda: ready)
    record = _TerminalLivenessRecord("parked", "codex", 123, None, None)
    with (
        patch.object(monitor, "_get_active_terminal_sessions", return_value=[record]),
        patch.object(monitor, "_expire_session", new=AsyncMock(return_value=True)) as expire,
        patch(
            "gobby.sessions.liveness_monitor.inspect_foreground_ownership",
            return_value=SimpleNamespace(state=OwnershipState.OWNERLESS),
        ),
    ):
        await monitor._check_sessions()
        expire.assert_not_awaited()
        assert "parked" not in monitor._recently_handled
        ready = True
        await monitor._check_sessions()
        expire.assert_awaited_once_with("parked")
        assert monitor._recently_handled["parked"] > 0
