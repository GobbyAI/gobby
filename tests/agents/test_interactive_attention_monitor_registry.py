"""Warm interactive pane-monitor registry acceptance tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.interactive_attention_monitor import InteractiveAttentionMonitor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal
from tests.agents.detection_test_support import replace_detection_manifest
from tests.agents.test_lifecycle_monitor import LifecycleRuntime
from tests.terminals.fakes import FakeRuntime, make_memory_terminal, runtime_registry


async def test_warm_pane_monitor_sees_content_edit(temp_db: HubDatabase) -> None:
    replace_detection_manifest(temp_db, "claude", "alpha")
    registry = DetectionManifestRegistry(temp_db, staleness_seconds=0.0)
    session_manager = Mock()
    attention_manager = Mock()
    attention_manager.get.return_value = None
    attention_manager.transition_async = AsyncMock()
    monitor = InteractiveAttentionMonitor(
        session_manager=session_manager,
        attention_manager=attention_manager,
        detection_registry=registry,
        registry=runtime_registry(FakeRuntime()),
    )

    await monitor._sync_interactive_attention("session-1", "claude", "alpha trust")
    assert attention_manager.transition_async.await_count == 1
    alpha_call = attention_manager.transition_async.await_args_list[0]

    replace_detection_manifest(temp_db, "claude", "beta")
    await monitor._sync_interactive_attention("session-1", "claude", "alpha trust")
    assert attention_manager.transition_async.await_args_list == [alpha_call]

    await monitor._sync_interactive_attention("session-1", "claude", "beta trust")

    assert attention_manager.transition_async.await_count == 2
    beta_call = attention_manager.transition_async.await_args_list[1]
    assert beta_call.kwargs["reason"] == "trust"
    assert beta_call.kwargs["fingerprint"] != alpha_call.kwargs["fingerprint"]
    assert monitor.detection_registry is registry


async def test_native_row_is_snapshotted_through_the_native_runtime(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_live_for_session has no backend filter, so the read must route per row."""
    replace_detection_manifest(temp_db, "claude", "alpha")
    detection_registry = DetectionManifestRegistry(temp_db, staleness_seconds=0.0)
    native_row = make_memory_terminal(backend="native")

    def get_live_for_session(_self: object, _session_id: str) -> Terminal:
        return native_row

    monkeypatch.setattr(
        "gobby.storage.terminals.TerminalManager.get_live_for_session",
        get_live_for_session,
    )
    session_manager = Mock()
    session_manager.db = Mock()
    session_manager.list.return_value = [
        SimpleNamespace(
            id="session-1",
            source="claude",
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            terminal_context={},
        )
    ]
    attention_manager = Mock()
    attention_manager.get.return_value = None
    attention_manager.list_blocked.return_value = []
    attention_manager.transition_async = AsyncMock()
    native_runtime = LifecycleRuntime(backend="native", snapshot_text="alpha trust")

    monitor = InteractiveAttentionMonitor(
        session_manager=session_manager,
        attention_manager=attention_manager,
        detection_registry=detection_registry,
        registry=runtime_registry(native_runtime),
    )

    await monitor._check_attention_panes(active_agent_sessions=set())

    assert native_runtime.snapshot_calls == [15]
    # The native runtime's text reached detection.
    assert attention_manager.transition_async.await_count == 1


async def test_native_session_without_terminal_context_still_gets_attention(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attention resolves the managed row even without terminal context."""
    replace_detection_manifest(temp_db, "claude", "alpha")
    detection_registry = DetectionManifestRegistry(temp_db, staleness_seconds=0.0)
    native_row = make_memory_terminal(backend="native")

    def get_live_for_session(_self: object, _session_id: str) -> Terminal:
        return native_row

    monkeypatch.setattr(
        "gobby.storage.terminals.TerminalManager.get_live_for_session",
        get_live_for_session,
    )
    session_manager = Mock()
    session_manager.db = Mock()
    session_manager.list.return_value = [
        SimpleNamespace(
            id="session-1",
            source="claude",
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            # A gterm-hosted session needs no terminal context to find its managed row.
            terminal_context={"parent_pid": 4242},
        )
    ]
    attention_manager = Mock()
    attention_manager.get.return_value = None
    attention_manager.list_blocked.return_value = []
    attention_manager.transition_async = AsyncMock()
    native_runtime = LifecycleRuntime(backend="native", snapshot_text="alpha trust")

    monitor = InteractiveAttentionMonitor(
        session_manager=session_manager,
        attention_manager=attention_manager,
        detection_registry=detection_registry,
        registry=runtime_registry(native_runtime),
    )

    await monitor._check_attention_panes(active_agent_sessions=set())

    assert native_runtime.snapshot_calls == [15]
    assert attention_manager.transition_async.await_count == 1
    state = attention_manager.transition_async.await_args_list[0].kwargs["state"]
    assert state == "blocked"
