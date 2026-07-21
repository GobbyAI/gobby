"""Escalation must not mutate stage rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, _de_escalation

pytestmark = pytest.mark.unit


def test_round_trip_preserves_row(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
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
    before_row = temp_db.fetchone(
        """
        SELECT stage_name, state, work_attempt_count, review_round_count, entered_at
          FROM task_stage_states
         WHERE task_id = %s AND stage_name = 'development'
        """,
        (task.id,),
    )
    assert before_row is not None
    before = dict(before_row)

    manager.escalate_task(task.id, reason="needs human")
    manager.de_escalate_task(task.id, reason="resolved")

    after_row = temp_db.fetchone(
        """
        SELECT stage_name, state, work_attempt_count, review_round_count, entered_at
          FROM task_stage_states
         WHERE task_id = %s AND stage_name = 'development'
        """,
        (task.id,),
    )
    assert after_row is not None
    after = dict(after_row)
    assert after == before


def test_de_escalate_can_reset_current_stage_work_attempts(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
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
    assert row is not None
    assert row["state"] == "in_progress"
    assert row["work_attempt_count"] == 0
    assert row["review_round_count"] == 2
    assert row["entered_at"] == datetime(2026, 5, 2, tzinfo=UTC)


def test_de_escalate_can_restore_stopped_approved_stage_from_history(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Restore approved stage")

    temp_db.execute(
        "DELETE FROM task_stage_states WHERE task_id = %s",
        (task.id,),
    )
    temp_db.execute(
        """
        INSERT INTO task_stage_states (
            task_id, stage_name, position, state, review_policy,
            work_attempt_count, review_round_count
        )
        VALUES (%s, 'expansion', 1, 'ready', 'required', 3, 0)
        """,
        (task.id,),
    )
    manager.lifecycle_events.record_lifecycle_event(
        task.id,
        "expansion:review_approved",
        "expansion:ready",
        "build_stop",
        by_actor="build",
    )

    manager.escalate_task(task.id, reason="expansion_work_failed:max")
    restored = manager.de_escalate_task(
        task.id,
        reason="coordinator repaired stopped approved stage",
        reset_stage_attempts=True,
        restore_stage_from_history=True,
    )

    row = temp_db.fetchone(
        """
        SELECT state, work_attempt_count, review_round_count
          FROM task_stage_states
         WHERE task_id = %s AND stage_name = 'expansion'
        """,
        (task.id,),
    )
    assert row is not None
    assert restored.is_escalated is False
    assert row["state"] == "review_approved"
    assert row["work_attempt_count"] == 0
    assert row["review_round_count"] == 0

    event = manager.lifecycle_events.list_events(task.id, newest_first=True, limit=1)[0]
    assert event.from_state == "expansion:review_approved"
    assert event.to_state == "expansion:review_approved"
    assert event.reason.startswith("reset_stage_work_attempts:")


def test_de_escalate_restore_stage_from_history_requires_build_stop_history(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="No restore history")

    temp_db.execute(
        "DELETE FROM task_stage_states WHERE task_id = %s",
        (task.id,),
    )
    temp_db.execute(
        """
        INSERT INTO task_stage_states (
            task_id, stage_name, position, state, review_policy,
            work_attempt_count, review_round_count
        )
        VALUES (%s, 'expansion', 1, 'ready', 'required', 3, 0)
        """,
        (task.id,),
    )

    manager.escalate_task(task.id, reason="expansion_work_failed:max")
    with pytest.raises(ValueError, match="not a build_stop"):
        manager.de_escalate_task(
            task.id,
            reason="coordinator repaired stopped approved stage",
            restore_stage_from_history=True,
        )

    assert manager.get_task(task.id).is_escalated is True


def test_de_escalate_restore_requires_latest_ready_transition_to_be_build_stop(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Latest restore history")
    temp_db.execute("DELETE FROM task_stage_states WHERE task_id = %s", (task.id,))
    temp_db.execute(
        """
        INSERT INTO task_stage_states (
            task_id, stage_name, position, state, review_policy,
            work_attempt_count, review_round_count
        )
        VALUES (%s, 'expansion', 1, 'ready', 'required', 3, 0)
        """,
        (task.id,),
    )
    manager.lifecycle_events.record_lifecycle_event(
        task.id,
        "expansion:review_approved",
        "expansion:ready",
        "build_stop",
        by_actor="build",
    )
    manager.lifecycle_events.record_lifecycle_event(
        task.id,
        "expansion:in_progress",
        "expansion:ready",
        "retry",
        by_actor="system",
    )
    manager.escalate_task(task.id, reason="expansion_work_failed:max")

    with pytest.raises(ValueError, match="latest transition into 'ready'"):
        manager.de_escalate_task(
            task.id,
            reason="attempt stale restoration",
            restore_stage_from_history=True,
        )

    assert manager.get_task(task.id).is_escalated is True
    stage_state = manager.stage_states.get(task.id, "expansion")
    assert stage_state is not None
    assert stage_state.state == "ready"


def test_de_escalate_rolls_back_claim_release_when_attempt_reset_fails(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Atomic de-escalation")
    manager.escalate_task(task.id, reason="development_max_work_attempts")

    def fail_reset(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("reset failed")

    monkeypatch.setattr(
        _de_escalation,
        "_reset_stage_work_attempts_for_de_escalation",
        fail_reset,
    )

    with pytest.raises(RuntimeError, match="reset failed"):
        manager.de_escalate_task(
            task.id,
            reason="force rollback",
            reset_stage_attempts=True,
        )

    persisted = manager.get_task(task.id)
    assert persisted.is_escalated is True
    assert persisted.escalation_reason == "development_max_work_attempts"


@pytest.mark.parametrize(
    "escalation_reason",
    ["epic_qa_work_failed:max", "epic_qa_max_work_attempts"],
)
def test_de_escalate_resets_exhausted_stage_named_by_escalation_reason(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
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
            (task.id, "epic_qa", 1, "ready", 4),
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
    assert rows["epic_qa"] == 0
    assert rows["merge"] == 0
