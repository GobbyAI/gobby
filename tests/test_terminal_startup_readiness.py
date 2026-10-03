"""Terminal monitors leave startup recovery in charge until readiness is published."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.sessions.liveness_monitor import SessionLivenessMonitor, _TerminalLivenessRecord
from gobby.terminal_ownership import OwnershipState
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminals.fakes import FakeRuntime, runtime_registry


async def test_interactive_attention_waits_for_startup_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = False
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=MagicMock(),
        registry=runtime_registry(FakeRuntime()),
        startup_ready=lambda: ready,
    )
    events: list[object] = []

    async def list_runs(_arm: object) -> list[object]:
        events.append("listed")
        return []

    async def check_attention(*, active_runs: list[object]) -> None:
        events.append(("checked", active_runs))

    monkeypatch.setattr(monitor, "_list_active_runs", list_runs)
    monkeypatch.setattr(monitor, "_check_attention_panes", check_attention)
    await monitor._check_attention()
    assert events == []
    ready = True
    await monitor._check_attention()
    assert events == ["listed", ("checked", [])]


async def test_session_liveness_waits_for_startup_reconciliation() -> None:
    ready = False
    monitor = SessionLivenessMonitor(session_storage=MagicMock(), startup_ready=lambda: ready)
    machine_id = "21000000-0000-4000-8000-000000000009"
    updated_at = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    # Expiry is conditional on a snapshot recorded by this machine.
    record = _TerminalLivenessRecord(
        session_id="parked",
        parent_pid=123,
        tmux_pane=None,
        machine_id=machine_id,
        updated_at=updated_at,
    )
    with (
        patch("gobby.sessions.liveness_monitor.get_machine_id", return_value=machine_id),
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
        expire.assert_awaited_once_with("parked", active_expiry=(machine_id, updated_at))
        assert monitor._recently_handled["parked"] > 0
