from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.storage.tasks._crud import list_automation_candidates, sweep_stale_claims
from gobby.storage.tasks._models import Isolation
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit


def _make_session(temp_db, sample_project, session_id: str, status: str) -> None:
    now = datetime.now(UTC).isoformat()
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            f"ext-{session_id}",
            "machine-test",
            "claude",
            sample_project["id"],
            status,
            now,
        ),
    )


def _claimed_task(temp_db, sample_project, *, claimed_by: str, stage_state: str = "ready"):
    task = create_task(
        temp_db,
        sample_project,
        title=f"Claimed by {claimed_by}",
        category="test",
        task_type="task",
    )
    initialize_manifest(temp_db, task.id, [spec("planning", 0)])
    set_stage_state(temp_db, task.id, "planning", stage_state)
    temp_db.execute(
        "UPDATE tasks SET allow_automation = TRUE, isolation = ?, claimed_by_session_id = ? WHERE id = ?",
        (Isolation.none.value, claimed_by, task.id),
    )
    return task


def _claim(temp_db, task_id: str) -> str | None:
    row = temp_db.fetchone("SELECT claimed_by_session_id FROM tasks WHERE id = ?", (task_id,))
    return row["claimed_by_session_id"] if row else None


def test_sweep_reclaims_task_claimed_by_inactive_session(temp_db, sample_project) -> None:
    _make_session(temp_db, sample_project, "sess-dead", "expired")
    task = _claimed_task(temp_db, sample_project, claimed_by="sess-dead")

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed >= 1
    assert _claim(temp_db, task.id) is None
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id in candidate_ids


def test_sweep_keeps_task_claimed_by_active_session(temp_db, sample_project) -> None:
    _make_session(temp_db, sample_project, "sess-live", "active")
    task = _claimed_task(temp_db, sample_project, claimed_by="sess-live")

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, task.id) == "sess-live"
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id not in candidate_ids


def test_sweep_keeps_task_claimed_by_paused_session(temp_db, sample_project) -> None:
    # A paused session is idle but alive (e.g. an interactive session
    # between turns); its claim must not be reclaimed.
    _make_session(temp_db, sample_project, "sess-paused", "paused")
    task = _claimed_task(temp_db, sample_project, claimed_by="sess-paused")

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, task.id) == "sess-paused"


def test_sweep_skips_closed_and_escalated_tasks(temp_db, sample_project) -> None:
    _make_session(temp_db, sample_project, "sess-dead", "expired")
    closed = _claimed_task(temp_db, sample_project, claimed_by="sess-dead")
    escalated = _claimed_task(temp_db, sample_project, claimed_by="sess-dead")
    now = datetime.now(UTC).isoformat()
    temp_db.execute("UPDATE tasks SET closed_at = ? WHERE id = ?", (now, closed.id))
    temp_db.execute(
        "UPDATE tasks SET escalated_at = ?, is_escalated = TRUE WHERE id = ?",
        (now, escalated.id),
    )

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, closed.id) == "sess-dead"
    assert _claim(temp_db, escalated.id) == "sess-dead"
