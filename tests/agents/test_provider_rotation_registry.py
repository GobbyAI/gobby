"""Provider-rotation registry refresh acceptance tests."""

from __future__ import annotations

from unittest.mock import Mock

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.provider_rotation import get_failed_providers_for_task
from gobby.agents.stall_classifier import StallClassifier
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import replace_detection_manifest


def test_rotation_classifier_sees_content_edit(temp_db: HubDatabase) -> None:
    replace_detection_manifest(temp_db, "claude", "alpha")
    registry = DetectionManifestRegistry(temp_db, staleness_seconds=0.0)
    classifier = StallClassifier(registry)
    agent_run_manager = Mock()
    agent_run_manager.db.fetchall.return_value = [
        {"provider": "claude", "error": "alpha unavailable"}
    ]

    assert get_failed_providers_for_task("task-1", agent_run_manager, classifier=classifier) == [
        "claude"
    ]

    replace_detection_manifest(temp_db, "claude", "beta")

    assert get_failed_providers_for_task("task-1", agent_run_manager, classifier=classifier) == []
    agent_run_manager.db.fetchall.return_value = [
        {"provider": "claude", "error": "beta unavailable"}
    ]
    assert get_failed_providers_for_task("task-1", agent_run_manager, classifier=classifier) == [
        "claude"
    ]
