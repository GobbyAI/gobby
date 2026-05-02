"""Phase 5 first-class escalation transition contracts."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_escalate_round_trip(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Escalate me")

    escalated = manager.escalate_task(task.id, reason="blocked by operator")
    row = temp_db.fetchone(
        "SELECT is_escalated, escalated_at, escalation_reason FROM tasks WHERE id = ?",
        (task.id,),
    )
    assert row["is_escalated"] == 1
    assert row["escalated_at"] is not None
    assert row["escalation_reason"] == "blocked by operator"
    assert escalated.is_escalated is True

    de_escalated = manager.de_escalate_task(task.id, reason="operator cleared")
    row = temp_db.fetchone(
        "SELECT is_escalated, escalated_at, escalation_reason FROM tasks WHERE id = ?",
        (task.id,),
    )
    assert row["is_escalated"] == 0
    assert row["escalated_at"] is None
    assert row["escalation_reason"] is None
    assert de_escalated.is_escalated is False

