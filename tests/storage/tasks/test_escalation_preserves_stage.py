"""Escalation must not mutate stage rows."""

from __future__ import annotations

import pytest

from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_round_trip_preserves_row(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Preserve stage")

    temp_db.execute(
        "DELETE FROM task_stage_states WHERE task_id = %s",
        (task.id,),
    )
    temp_db.execute(
        """
        INSERT INTO task_stage_states (
            task_id, stage_name, position, state, review_policy, entered_at,
            work_attempt_count, review_round_count
        )
        VALUES (%s, 'development', 1, 'in_progress', 'required',
                '2026-05-02T00:00:00+00:00', 2, 0)
        """,
        (task.id,),
    )
    before = dict(
        temp_db.fetchone(
            """
            SELECT stage_name, state, work_attempt_count, review_round_count, entered_at
              FROM task_stage_states
             WHERE task_id = %s AND stage_name = 'development'
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
             WHERE task_id = %s AND stage_name = 'development'
            """,
            (task.id,),
        )
    )
    assert after == before


def test_de_escalate_can_reset_current_stage_work_attempts(temp_db, sample_project) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Reset stage attempts")

    temp_db.execute(
        "DELETE FROM task_stage_states WHERE task_id = %s",
        (task.id,),
    )
    temp_db.execute(
        """
        INSERT INTO task_stage_states (
            task_id, stage_name, position, state, review_policy, entered_at,
            work_attempt_count, review_round_count
        )
        VALUES (%s, 'development', 1, 'in_progress', 'required',
                '2026-05-02T00:00:00+00:00', 4, 2)
        """,
        (task.id,),
    )

    manager.escalate_task(task.id, reason="development_max_work_attempts")
    manager.de_escalate_task(
        task.id,
        reason="coordinator fixed blocker",
        reset_stage_attempts=True,
    )

    row = temp_db.fetchone(
        """
        SELECT state, work_attempt_count, review_round_count, entered_at
          FROM task_stage_states
         WHERE task_id = %s AND stage_name = 'development'
        """,
        (task.id,),
    )
    assert row["state"] == "in_progress"
    assert row["work_attempt_count"] == 0
    assert row["review_round_count"] == 2
    assert row["entered_at"] == "2026-05-02T00:00:00+00:00"


@pytest.mark.parametrize(
    "escalation_reason",
    ["holistic_qa_work_failed:max", "holistic_qa_max_work_attempts"],
)
def test_de_escalate_resets_exhausted_stage_named_by_escalation_reason(
    temp_db,
    sample_project,
    escalation_reason: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Reset exhausted stage")

    temp_db.execute(
        "DELETE FROM task_stage_states WHERE task_id = %s",
        (task.id,),
    )
    temp_db.executemany(
        """
        INSERT INTO task_stage_states (
            task_id, stage_name, position, state, review_policy, entered_at,
            work_attempt_count, review_round_count
        )
        VALUES (%s, %s, %s, %s, 'required', '2026-05-02T00:00:00+00:00', %s, 0)
        """,
        [
            (task.id, "development", 0, "ready", 1),
            (task.id, "holistic_qa", 1, "ready", 4),
            (task.id, "merge", 2, "ready", 0),
        ],
    )

    manager.escalate_task(task.id, reason=escalation_reason)
    manager.de_escalate_task(
        task.id,
        reason="coordinator fixed blocker",
        reset_stage_attempts=True,
    )

    rows = {
        row["stage_name"]: row["work_attempt_count"]
        for row in temp_db.fetchall(
            """
            SELECT stage_name, work_attempt_count
              FROM task_stage_states
             WHERE task_id = %s
            """,
            (task.id,),
        )
    }
    assert rows["development"] == 1
    assert rows["holistic_qa"] == 0
    assert rows["merge"] == 0
