from __future__ import annotations

import logging
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from gobby.sync_registry import sync_bundled_content_to_db

SYNC_TARGETS = (
    "gobby.skills.sync.sync_bundled_skills",
    "gobby.prompts.sync.sync_bundled_prompts",
    "gobby.agents.sync.sync_bundled_agents",
    "gobby.workflows.sync_pipelines.sync_bundled_pipelines",
    "gobby.workflows.sync_rules.sync_bundled_rules",
    "gobby.workflows.sync_variables.sync_bundled_variables",
    "gobby.storage.build_profiles.sync_bundled_build_profiles",
    "gobby.agents.detection.registry.sync_bundled_detection_manifests",
)


def _mock_sync_targets(*, changed_target: str | None = None) -> ExitStack:
    stack = ExitStack()
    for target in SYNC_TARGETS:
        synced = 1 if target == changed_target else 0
        stack.enter_context(
            patch(
                target,
                return_value={"synced": synced, "updated": 0, "skipped": 1, "errors": []},
            )
        )
    stack.enter_context(patch("gobby.utils.dev.is_dev_mode", return_value=True))
    return stack


def test_noop_bundled_sync_emits_debug_without_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        _mock_sync_targets(),
        caplog.at_level(logging.DEBUG, logger="gobby.sync_registry"),
    ):
        result = sync_bundled_content_to_db(MagicMock())

    assert result["total_synced"] == 0
    noop_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "Bundled content sync made no database changes"
    )
    assert noop_record.levelno == logging.DEBUG
    assert not any(record.levelno == logging.INFO for record in caplog.records)


def test_changed_bundled_sync_emits_one_aggregate_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    changed_target = "gobby.skills.sync.sync_bundled_skills"
    with (
        _mock_sync_targets(changed_target=changed_target),
        caplog.at_level(logging.DEBUG, logger="gobby.sync_registry"),
    ):
        result = sync_bundled_content_to_db(MagicMock())

    assert result["total_synced"] == 1
    info_records = [record for record in caplog.records if record.levelno == logging.INFO]
    assert len(info_records) == 1
    aggregate = info_records[0]
    assert aggregate.getMessage() == "Bundled content sync changed database state"
    assert aggregate.__dict__["changed"] == 1
    assert aggregate.__dict__["content_types"] == {"skills": 1}
