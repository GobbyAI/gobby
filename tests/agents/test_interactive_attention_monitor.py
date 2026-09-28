"""Interactive attention polling across native terminal sessions."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.storage.agents import AgentRun
from gobby.terminals.host_client import HostConnectionLost, HostUnavailableError
from gobby.utils.logging import ThrottledLogger
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.test_lifecycle_monitor import LifecycleRuntime
from tests.terminals.fakes import make_memory_terminal, runtime_registry

pytestmark = pytest.mark.unit
DETECTION_REGISTRY = BundledDetectionRegistry()


def _monitor(session_manager: Mock, runtime: LifecycleRuntime) -> InteractiveAttentionMonitor:
    return InteractiveAttentionMonitor(
        detection_registry=DETECTION_REGISTRY,
        session_manager=session_manager,
        attention_manager=Mock(),
        registry=runtime_registry(runtime),
    )


@pytest.mark.asyncio
async def test_active_runs_are_paginated_on_worker_thread() -> None:
    session_manager = Mock(db=Mock())
    monitor = _monitor(session_manager, LifecycleRuntime(backend="native"))
    runs = [SimpleNamespace(id=f"run-{index}") for index in range(101)]
    calls: list[tuple[int, int]] = []
    worker_threads: set[int] = set()
    main_thread = threading.get_ident()

    def list_active_for_machine(machine_id: str, *, limit: int, offset: int) -> list[AgentRun]:
        del machine_id
        calls.append((limit, offset))
        worker_threads.add(threading.get_ident())
        return cast(list[AgentRun], runs[offset : offset + limit])

    with (
        patch("gobby.storage.agents.LocalAgentRunManager") as arm_class,
        patch.object(monitor, "_check_attention_panes", new_callable=AsyncMock),
    ):
        arm_class.return_value.list_active_for_machine.side_effect = list_active_for_machine
        await monitor._check_attention()

    assert calls == [(100, 0), (100, 100)]
    assert worker_threads and main_thread not in worker_threads


@pytest.mark.asyncio
async def test_interactive_sessions_use_cursor_pagination_on_worker_thread() -> None:
    session_manager = Mock(db=Mock())
    monitor = _monitor(session_manager, LifecycleRuntime(backend="native"))
    sessions = [
        SimpleNamespace(id=f"session-{index}", updated_at=datetime(2026, 1, 1, tzinfo=UTC))
        for index in range(101)
    ]
    pages = [sessions[:100], sessions[100:]]
    worker_threads: set[int] = set()
    main_thread = threading.get_ident()

    def list_sessions(**kwargs: object) -> list[SimpleNamespace]:
        worker_threads.add(threading.get_ident())
        assert kwargs["cursor_id"] == (None if len(pages) == 2 else sessions[99].id)
        return pages.pop(0)

    session_manager.list.side_effect = list_sessions
    assert await monitor._list_interactive_sessions() == sessions
    assert worker_threads and main_thread not in worker_threads


@pytest.mark.parametrize(
    "outage", [HostConnectionLost("closed"), HostUnavailableError("unavailable")]
)
@pytest.mark.asyncio
async def test_lost_host_skips_remaining_native_snapshots_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage: HostUnavailableError,
) -> None:
    sessions = [
        SimpleNamespace(id=f"session-{index}", source="claude", terminal_context={})
        for index in range(2)
    ]
    rows = {session.id: make_memory_terminal(backend="native") for session in sessions}
    monkeypatch.setattr(
        "gobby.storage.terminals.TerminalManager.get_live_for_session",
        lambda _manager, session_id: rows[session_id],
    )
    session_manager = Mock(db=Mock())
    session_manager.list.return_value = sessions
    runtime = LifecycleRuntime(backend="native", snapshot_error=outage)
    monitor = _monitor(session_manager, runtime)
    cast(Mock, monitor._attention_manager).list_blocked.return_value = []
    monkeypatch.setattr(
        "gobby.agents.interactive_attention_monitor._host_outage_log", ThrottledLogger()
    )

    with (
        patch.object(monitor, "_sync_interactive_attention", new_callable=AsyncMock) as sync,
        caplog.at_level(logging.DEBUG, logger="gobby.agents.interactive_attention_monitor"),
    ):
        await monitor._check_attention_panes(active_runs=[])
        assert runtime.snapshot_calls == [15]
        assert sync.await_count == 0
        assert (
            len(
                [record for record in caplog.records if "native host unavailable" in record.message]
            )
            == 1
        )
        assert not [record for record in caplog.records if record.levelno >= logging.INFO]

        runtime.snapshot_error = None
        await monitor._check_attention_panes(active_runs=[])

    assert runtime.snapshot_calls == [15, 15, 15]
    assert sync.await_count == 2


@pytest.mark.asyncio
async def test_unexpected_capture_failure_still_reports_each_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = [SimpleNamespace(id=f"session-{index}", source="claude") for index in range(2)]
    rows = {session.id: make_memory_terminal(backend="native") for session in sessions}
    monkeypatch.setattr(
        "gobby.storage.terminals.TerminalManager.get_live_for_session",
        lambda _manager, session_id: rows[session_id],
    )
    session_manager = Mock(db=Mock())
    session_manager.list.return_value = sessions
    runtime = LifecycleRuntime(backend="native", snapshot_error=RuntimeError("bad snapshot"))
    monitor = _monitor(session_manager, runtime)
    cast(Mock, monitor._attention_manager).list_blocked.return_value = []

    with caplog.at_level(logging.WARNING, logger="gobby.agents.interactive_attention_monitor"):
        await monitor._check_attention_panes(active_runs=[])

    assert runtime.snapshot_calls == [15, 15]
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 2
