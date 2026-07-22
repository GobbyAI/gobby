"""Warm interactive pane-monitor registry acceptance tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.tmux.pane_monitor import TmuxPaneMonitor
from gobby.config.tmux import TmuxConfig
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import replace_detection_manifest


async def test_warm_pane_monitor_sees_content_edit(temp_db: HubDatabase) -> None:
    replace_detection_manifest(temp_db, "claude", "alpha")
    registry = DetectionManifestRegistry(temp_db, staleness_seconds=0.0)
    session_manager = Mock()
    session_manager.get.return_value = SimpleNamespace(source="claude")
    attention_manager = Mock()
    attention_manager.get.return_value = None
    attention_manager.transition_async = AsyncMock()
    monitor = TmuxPaneMonitor(
        session_end_callback=AsyncMock(),
        config=TmuxConfig(),
        session_manager=session_manager,
        attention_manager=attention_manager,
        detection_registry=registry,
    )

    await monitor._sync_interactive_attention("session-1", "alpha trust")
    assert attention_manager.transition_async.await_count == 1

    replace_detection_manifest(temp_db, "claude", "beta")
    await monitor._sync_interactive_attention("session-1", "alpha trust")
    await monitor._sync_interactive_attention("session-1", "beta trust")

    assert attention_manager.transition_async.await_count == 2
    assert monitor.detection_registry is registry
