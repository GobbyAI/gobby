"""Escalation must not mutate stage rows."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_round_trip_preserves_row(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Preserve stage")

    temp_db.execute(
        "DELETE FROM task_stage_states WHERE task_id = ?",
        (task.id,),
    )
    temp_db.execute(
        """
        INSERT INTO task_stage_states (
            task_id, stage_name, position, state, review_policy, entered_at,
            work_attempt_count, review_round_count
        )
        VALUES (?, 'development', 1, 'in_progress', 'required',
                '2026-05-02T00:00:00+00:00', 2, 0)
        """,
        (task.id,),
    )
    before = dict(
        temp_db.fetchone(
            """
            SELECT stage_name, state, work_attempt_count, review_round_count, entered_at
              FROM task_stage_states
             WHERE task_id = ? AND stage_name = 'development'
            """,
            (task.id,),
        )
    )

    manager.escalate_task(task.id, reason="needs human")
    manager.de_escalate_task(task.id, reason="resolved")

    after = dict(
        temp_db.fetchone(
            """
            SELECT stage_name, state, work_attempt_count, review_round_count, entered_at
              FROM task_stage_states
             WHERE task_id = ? AND stage_name = 'development'
            """,
            (task.id,),
        )
    )
    assert after == before
