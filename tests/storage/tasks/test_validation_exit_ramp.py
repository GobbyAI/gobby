"""Storage close concurrency and historical validation metadata coverage."""

from __future__ import annotations

from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import (
    LocalTaskManager,
    TaskStaleStateError,
    _transitions,
)

UNKNOWN_TASK_ID = "99999999-9999-9999-9999-999999999999"


def test_close_missing_task_raises_not_found(temp_db: HubDatabase) -> None:
    manager = LocalTaskManager(temp_db)

    with pytest.raises(ValueError, match="not found"):
        manager.close_task(UNKNOWN_TASK_ID)


def test_close_existing_task_with_changed_updated_at_raises_stale(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    snapshot = manager.create_task(
        sample_project["id"],
        "Concurrent close",
        validation_criteria="Test task completion is observable.",
    )
    manager.update_task(snapshot.id, title="Changed concurrently")

    with pytest.raises(TaskStaleStateError):
        manager.close_task(snapshot.id, expected_updated_at=snapshot.updated_at)


def test_deliberate_close_of_escalated_task_succeeds_and_clears_escalation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A caller that read the escalated row may close it; only racing closes lose."""
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Escalated resolution close",
        validation_criteria="Test task completion is observable.",
    )
    manager.escalate_task(task.id, reason="ready for QA review")
    snapshot = manager.get_task(task.id)

    closed = _transitions.close_task(
        temp_db,
        task.id,
        expected_updated_at=snapshot.updated_at,
    )

    assert closed.closed_at is not None
    assert closed.is_escalated is False
    assert closed.escalated_at is None

    with pytest.raises(TaskStaleStateError):
        _transitions.close_task(
            temp_db,
            task.id,
            expected_updated_at=snapshot.updated_at,
        )


def test_manual_escalation_reopen_preserves_historical_review_fail_count(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Manual reopen",
        validation_criteria="Test task completion is observable.",
    )
    # Below the rejection limit; an exhausted epoch reopens only through the audited reset.
    manager.update_task(task.id, review_fail_count=2)
    manager.escalate_task(task.id, reason="manual review")

    reopened = manager.reopen_task(task.id, reason="review resolved")

    assert reopened.review_fail_count == 2
    assert reopened.is_escalated is False


def test_repeated_close_preserves_terminal_timestamps(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Idempotent close",
        validation_criteria="Test task completion is observable.",
    )
    first = manager.close_task(task.id, reason="done")
    second = manager.close_task(task.id, reason="moot")

    assert second.closed_at == first.closed_at
    assert second.updated_at == first.updated_at
    assert second.closed_reason == first.closed_reason == "done"
