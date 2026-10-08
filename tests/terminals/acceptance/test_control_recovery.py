"""Real control interruption must recover without a per-pane traceback burst."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.terminals.host_client import HostClient
from gobby.utils.logging import ThrottledLogger
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminals.acceptance.conftest import (
    AcceptanceHost,
    emit_marker,
    spawn_native,
    wait_for_text,
)
from tests.terminals.fakes import runtime_registry

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_control_interruption_recovers_multiple_panes_without_alert_burst(
    native_host: AcceptanceHost,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two real socket outages preserve the host and recover every original pane."""
    panes = [await spawn_native(native_host) for _ in range(3)]
    manager = native_host.manager
    original_identity = (manager.host_pid, manager.host_epoch, manager.restart_count)
    assert original_identity[0] is not None
    assert original_identity[1] is not None

    # Drive the existing health loop one tick at a time; socket IO is real.
    assert manager._health_task is not None
    manager._health_task.cancel()
    with suppress(asyncio.CancelledError):
        await manager._health_task
    ticks: asyncio.Queue[None] = asyncio.Queue()
    permits: asyncio.Queue[None] = asyncio.Queue()

    async def next_tick(_delay: float) -> None:
        await ticks.put(None)
        await permits.get()

    monkeypatch.setattr(manager, "_sleep", next_tick)
    manager._health_task = asyncio.create_task(manager._health_loop())
    async with asyncio.timeout(10):
        await ticks.get()

    sessions = [
        SimpleNamespace(id=f"acceptance-{index}", source="claude", terminal_context={})
        for index in range(len(panes))
    ]
    rows = {session.id: pane.terminal for session, pane in zip(sessions, panes, strict=True)}
    session_manager = Mock(db=Mock())
    session_manager.list.return_value = sessions
    attention_manager = Mock()
    attention_manager.list_blocked.return_value = []
    monitor = InteractiveAttentionMonitor(
        detection_registry=BundledDetectionRegistry(),
        session_manager=session_manager,
        attention_manager=attention_manager,
        registry=runtime_registry(native_host.runtime),
    )
    # This acceptance proof concerns capture; attention classification is separate.
    monitor._capacity_recovery = None
    sync = AsyncMock()
    monkeypatch.setattr(monitor, "_sync_interactive_attention", sync)
    monkeypatch.setattr(
        "gobby.storage.terminals.TerminalManager.get_live_for_session",
        lambda _manager, session_id: rows[session_id],
    )
    monkeypatch.setattr(
        "gobby.agents.interactive_attention_monitor._host_outage_log", ThrottledLogger()
    )

    with (
        caplog.at_level(logging.DEBUG),
        patch.object(
            native_host.runtime, "snapshot", wraps=native_host.runtime.snapshot
        ) as capture,
    ):
        for outage in range(2):
            client = cast(HostClient, manager._client)
            await client.close()
            assert client.closed
            capture.reset_mock()
            sync.reset_mock()
            await monitor._check_attention_panes(active_runs=[])
            assert capture.await_count == 1
            assert sync.await_count == 0

            await permits.put(None)
            async with asyncio.timeout(10):
                await ticks.get()
            assert manager._client is not client
            assert manager.native_available
            assert manager.last_error is None
            assert (
                manager.host_pid,
                manager.host_epoch,
                manager.restart_count,
            ) == original_identity

            markers = []
            for pane in panes:
                marker = await emit_marker(pane, f"RECOVERED-{outage}")
                await wait_for_text(pane, marker, description="the original pane after reconnect")
                markers.append(marker)
            capture.reset_mock()
            await monitor._check_attention_panes(active_runs=[])
            assert capture.await_count == len(panes)
            assert sync.await_count == len(panes)
            for call, marker in zip(sync.await_args_list, markers, strict=True):
                assert marker in call.args[2]

            warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
            assert len(warnings) == outage + 1
            assert all(
                "gterm control probe failed; reconnecting live host" in r.message for r in warnings
            )
            assert all(record.exc_info is None for record in warnings)
