"""The generic task-state contract is independent of review progress."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg.errors import CheckViolation

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.task_dependencies import TaskDependencyManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.state_semantics import (
    projected_task_state,
    serialize_task_state,
    validate_task_state,
)
from gobby.utils.machine_id import require_machine_id


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({}, "open", id="unclaimed"),
        pytest.param({"claimed_by_session_id": "owner"}, "in_progress", id="claimed"),
        pytest.param(
            {"claimed_by_session_id": "owner", "active_blocked_by": {"dependency"}},
            "blocked",
            id="blocked-claim",
        ),
        pytest.param({"blocked_by_merge": True}, "blocked", id="merge-hold"),
        pytest.param(
            {"hold_state": "needs_human", "active_blocked_by": {"dependency"}},
            "needs_human",
            id="human-hold",
        ),
        pytest.param(
            {"labels": ["awaiting-human-review"], "claimed_by_session_id": "owner"},
            "needs_human",
            id="existing-human-label",
        ),
        pytest.param(
            {"hold_state": "deferred", "follow_up_at": datetime(2020, 1, 1, tzinfo=UTC)},
            "deferred",
            id="elapsed-follow-up-does-not-auto-release",
        ),
        pytest.param(
            {"is_escalated": True, "hold_state": "deferred"},
            "escalated",
            id="escalation-precedes-hold",
        ),
        pytest.param(
            {"closed_at": datetime(2026, 1, 1, tzinfo=UTC), "is_escalated": True},
            "closed",
            id="closure-precedes-escalation",
        ),
    ],
)
@pytest.mark.unit
def test_state_precedence_and_claim_transitions(fields: dict[str, object], expected: str) -> None:
    task = SimpleNamespace(**fields)
    assert projected_task_state(task) == expected
    state = serialize_task_state(task)
    assert state["name"] == expected
    assert state["is_blocked"] is (expected == "blocked")
    assert projected_task_state({"state": state}) == expected


@pytest.mark.parametrize("review_status", ["pending", "valid", "invalid", "error"])
@pytest.mark.unit
def test_review_progress_does_not_change_task_state(review_status: str) -> None:
    task = SimpleNamespace(claimed_by_session_id="owner", review_status=review_status)
    assert projected_task_state(task) == "in_progress"
    task.claimed_by_session_id = None
    assert projected_task_state(task) == "open"


@pytest.mark.parametrize("state", ["ready", "needs_review", "review_approved", "done", "claimed"])
@pytest.mark.unit
def test_retired_state_values_are_rejected(state: str) -> None:
    with pytest.raises(ValueError, match="Unknown task state"):
        validate_task_state(state)


@pytest.fixture
def state_owner(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    return (
        SessionManager(temp_db)
        .register(
            external_id="task-state-owner",
            machine_id=require_machine_id(),
            source="codex",
            project_id=sample_project["id"],
        )
        .id
    )


@pytest.mark.integration
def test_query_and_surface_state_parity(
    temp_db: HubDatabase, sample_project: dict[str, Any], state_owner: str
) -> None:
    tasks = LocalTaskManager(temp_db)
    project_id: str = sample_project["id"]
    states = ("open", "in_progress", "blocked", "needs_human", "deferred", "escalated", "closed")
    created = {
        name: tasks.create_task(
            project_id, title=name, validation_criteria="Task-state surfaces agree."
        )
        for name in states
    }
    for name in ("in_progress", "blocked"):
        tasks.claim_task(created[name].id, state_owner)
    TaskDependencyManager(temp_db).add_dependency(created["blocked"].id, created["open"].id)
    tasks.add_label(created["needs_human"].id, "awaiting-human-review")
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET hold_state = 'deferred', follow_up_at = %s WHERE id = %s",
            (datetime(2020, 1, 1, tzinfo=UTC), created["deferred"].id),
        )
    tasks.escalate_task(created["escalated"].id, reason="Operator decision required")
    tasks.close_task(created["closed"].id, reason="done")

    assert tasks.count_by_state(project_id) == dict.fromkeys(states, 1)
    for name, task in created.items():
        selected = tasks.list_tasks(project_id=project_id, state=name)
        assert [item.id for item in selected] == [task.id]
        assert tasks.count_tasks(project_id=project_id, state=name) == 1
        assert selected[0].to_dict()["state"]["name"] == name
        assert selected[0].to_brief()["state"]["name"] == name
        assert projected_task_state(tasks.get_task(task.id)) == name
    assert {task.id for task in tasks.list_ready_tasks(project_id=project_id)} == {
        created["open"].id,
        created["in_progress"].id,
    }
    assert [task.id for task in tasks.list_blocked_tasks(project_id=project_id)] == [
        created["blocked"].id
    ]
    assert tasks.count_blocked_tasks(project_id=project_id) == 1

    tasks.close_task(created["open"].id, reason="done")
    assert projected_task_state(tasks.get_task(created["blocked"].id)) == "in_progress"
    assert tasks.count_tasks(project_id=project_id, state="blocked") == 0
    tasks.release_task_claim(created["blocked"].id)
    assert projected_task_state(tasks.get_task(created["blocked"].id)) == "open"
    row = temp_db.fetchone("SELECT state_bucket FROM tasks WHERE id = %s", (created["blocked"].id,))
    assert row is not None and row["state_bucket"] == "open"


@pytest.mark.parametrize(
    "assignment",
    [
        "hold_state = 'deferred'",
        "hold_state = 'ready'",
        "state_bucket = 'ready'",
        "closed_at = now(), closed_reason = NULL",
        "closed_at = now(), closed_reason = 'completed'",
    ],
)
@pytest.mark.integration
def test_state_storage_invariants(
    temp_db: HubDatabase, sample_project: dict[str, Any], assignment: str
) -> None:
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        sample_project["id"], title="Invariant probe", validation_criteria="Invalid writes fail."
    )
    with pytest.raises(CheckViolation), temp_db.transaction() as conn:
        # These literal test assignments deliberately exercise the database boundary.
        conn.execute(f"UPDATE tasks SET {assignment} WHERE id = %s", (task.id,))
    unchanged = tasks.get_task(task.id)
    assert unchanged.hold_state is None and unchanged.closed_at is None
