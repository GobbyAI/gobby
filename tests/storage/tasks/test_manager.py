"""Tests for LocalTaskManager task creation helpers."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_create_task_is_metadata_only(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)

    task = manager.create_task(
        project_id=sample_project["id"],
        title="Metadata-only task",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )

    assert manager.stage_states.list_for_task(task.id) == []
    assert manager.stage_states.current_stage(task.id) is None


def test_initialize_task_manifest_supports_stage_override_with_caps(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Override stages",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )

    manager.initialize_task_manifest(
        task.id,
        stage_names=["development", "pr", "merge"],
        stage_caps=[{"stage_name": "development", "max_review_rounds": 2}],
    )

    rows = manager.stage_states.list_for_task(task.id)
    assert [row.stage_name for row in rows] == ["development", "pr", "merge"]
    assert rows[0].max_review_rounds == 2


def test_initialize_task_manifest_rejects_unknown_stage_override(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Bad stages",
        task_type="task",
        validation_criteria="Test task completion is observable.",
    )

    with pytest.raises(ValueError, match="Unknown stage 'missing'"):
        manager.initialize_task_manifest(task.id, stage_names=["development", "missing"])


def test_update_task_persists_normalized_validation_criteria(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Normalize criteria",
        task_type="task",
        validation_criteria="Initial criterion.",
    )

    updated = manager.update_task(
        task.id,
        validation_criteria="  Updated observable criterion.  ",
    )

    assert updated.validation_criteria == "Updated observable criterion."


def test_update_task_changes_only_escalation_reason(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Correct escalation reason",
        task_type="task",
        validation_criteria="The escalation reason is current.",
    )
    escalated = manager.escalate_task(task.id, reason="Stale reason")

    updated = manager.update_task(task.id, escalation_reason="Current reason")

    assert updated.escalation_reason == "Current reason"
    assert updated.escalated_at == escalated.escalated_at
    assert updated.is_escalated is True


def test_update_task_rejects_escalation_reason_for_non_escalated_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Reject orphan escalation reason",
        task_type="task",
        validation_criteria="No orphan escalation reason is stored.",
    )

    with pytest.raises(
        ValueError,
        match="Cannot update escalation_reason for a task that is not escalated",
    ):
        manager.update_task(task.id, escalation_reason="Orphan reason")

    assert manager.get_task(task.id).escalation_reason is None


def test_update_task_only_refetches_once_for_non_contract_fields(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Original title",
        task_type="task",
        validation_criteria="The title is updated.",
    )
    original_get_task = manager.get_task
    get_calls = 0

    def tracked_get_task(task_id: str) -> Any:
        nonlocal get_calls
        get_calls += 1
        return original_get_task(task_id)

    monkeypatch.setattr(manager, "get_task", tracked_get_task)

    updated = manager.update_task(task.id, title="Updated title")

    assert updated.title == "Updated title"
    assert get_calls == 1
