"""Terminal monitors leave startup recovery in charge until readiness is published."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.sessions.liveness_monitor import SessionLivenessMonitor, _TerminalLivenessRecord
from gobby.terminal_ownership import OwnershipState
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminals.fakes import FakeRuntime, runtime_registry


async def test_interactive_attention_waits_for_startup_reconciliation() -> None:
    ready = False
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=MagicMock(),
        registry=runtime_registry(FakeRuntime()),
        startup_ready=lambda: ready,
    )
    with (
        patch.object(monitor, "_list_active_runs", new=AsyncMock(return_value=[])) as runs,
        patch.object(monitor, "_check_attention_panes", new=AsyncMock()) as attention,
    ):
        await monitor._check_attention()
        runs.assert_not_awaited()
        attention.assert_not_awaited()
        ready = True
        await monitor._check_attention()
    runs.assert_awaited_once()
    attention.assert_awaited_once_with(active_runs=[])


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
