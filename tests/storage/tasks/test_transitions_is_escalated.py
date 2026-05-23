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


def test_de_escalate_releases_stale_claim(temp_db, sample_project, session_manager) -> None:
    manager = LocalTaskManager(temp_db)
    session = session_manager.register(
        external_id="de-escalate-owner-ext",
        machine_id="test-machine",
        source="codex",
        project_id=sample_project["id"],
    )
    task = manager.create_task(project_id=sample_project["id"], title="Escalate me")

    manager.escalate_task(task.id, reason="blocked by operator")
    temp_db.execute(
        """
        UPDATE tasks
           SET claimed_by_session_id = ?,
               assignee = ?
         WHERE id = ?
        """,
        (session.id, session.id, task.id),
    )

    de_escalated = manager.de_escalate_task(task.id, reason="operator cleared")
    row = temp_db.fetchone(
        "SELECT claimed_by_session_id, assignee FROM tasks WHERE id = ?",
        (task.id,),
    )

    assert row["claimed_by_session_id"] is None
    assert row["assignee"] is None
    assert de_escalated.claimed_by_session_id is None
    assert de_escalated.assignee is None
