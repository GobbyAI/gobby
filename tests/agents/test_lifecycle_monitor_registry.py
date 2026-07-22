"""Warm lifecycle-monitor registry acceptance tests."""

from __future__ import annotations

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import replace_detection_manifest


def test_warm_monitor_sees_content_edit(temp_db: HubDatabase) -> None:
    replace_detection_manifest(temp_db, "claude", "alpha")
    registry = DetectionManifestRegistry(temp_db, staleness_seconds=0.0)
    monitor = AgentLifecycleMonitor(
        agent_run_manager=LocalAgentRunManager(temp_db),
        db=temp_db,
        tmux_config=TmuxConfig(),
        detection_registry=registry,
    )

    prompt = monitor.prompt_detector.for_provider("claude")
    idle = monitor.idle_detector.for_provider("claude")
    stall = monitor.stall_classifier.for_provider("claude")
    assert prompt.detect_trust_prompt("alpha trust") is True
    assert idle.detect("alpha idle") == "idle"
    assert stall.is_provider_error("alpha unavailable") is True

    replace_detection_manifest(temp_db, "claude", "beta")

    assert monitor.prompt_detector.for_provider("claude") is prompt
    assert monitor.idle_detector.for_provider("claude") is idle
    assert monitor.stall_classifier.for_provider("claude") is stall
    assert prompt.detect_trust_prompt("alpha trust") is False
    assert prompt.detect_trust_prompt("beta trust") is True
    assert idle.detect("beta idle") == "idle"
    assert stall.is_provider_error("beta unavailable") is True
