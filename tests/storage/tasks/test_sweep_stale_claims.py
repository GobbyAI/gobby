from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from gobby.sessions.compact_continuation import (
    COMPACT_SELF_CONTINUE_FRESH_SECONDS,
    mark_compact_self_continuation_pending,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._automation import list_automation_candidates, sweep_stale_claims
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._models import Isolation, Task
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit

SESS_DEAD = str(uuid.uuid4())


def _make_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_id: str,
    status: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session_id,
            f"ext-{session_id[:8]}",
            "machine-test",
            "claude",
            sample_project["id"],
            status,
            now,
        ),
    )


def _claimed_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    claimed_by: str,
    stage_state: str = "ready",
    allow_automation: bool = True,
) -> Task:
    task = cast(
        Task,
        create_task(
            temp_db,
            sample_project,
            title=f"Claimed by {claimed_by}",
            category="test",
            task_type="task",
        ),
    )
    initialize_manifest(temp_db, task.id, [spec("planning", 0)])
    set_stage_state(temp_db, task.id, "planning", stage_state)
    temp_db.execute(
        "UPDATE tasks SET allow_automation = %s, isolation = %s, claimed_by_session_id = %s WHERE id = %s",
        (allow_automation, Isolation.none.value, claimed_by, task.id),
    )
    return task


def _claim(temp_db: HubDatabase, task_id: str) -> str | None:
    row = temp_db.fetchone("SELECT claimed_by_session_id FROM tasks WHERE id = %s", (task_id,))
    return row["claimed_by_session_id"] if row else None


@pytest.mark.parametrize("status", ["expired", "deleted"])
def test_sweep_reclaims_task_claimed_by_terminal_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    status: str,
) -> None:
    _make_session(temp_db, sample_project, SESS_DEAD, status)
    task = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed >= 1
    assert _claim(temp_db, task.id) is None
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id in candidate_ids


def test_missing_session_cannot_retain_task_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "active")
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)
    assert _claim(temp_db, task.id) == session_id

    temp_db.execute("DELETE FROM sessions WHERE id = %s", (session_id,))

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, task.id) is None


def test_sweep_reclaims_non_automation_task_claimed_by_inactive_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    _make_session(temp_db, sample_project, SESS_DEAD, "expired")
    task = _claimed_task(
        temp_db,
        sample_project,
        claimed_by=SESS_DEAD,
        allow_automation=False,
    )

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed >= 1
    assert _claim(temp_db, task.id) is None
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id not in candidate_ids


@pytest.mark.parametrize("status", ["active", "paused", "handoff_ready"])
def test_sweep_keeps_task_claimed_by_live_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    status: str,
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, status)
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, task.id) == session_id
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id not in candidate_ids


def test_sweep_keeps_claim_during_pending_compact_continuation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "expired")
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)
    assert mark_compact_self_continuation_pending(temp_db, session_id)

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed == 0
    assert _claim(temp_db, task.id) == session_id


def test_sweep_reclaims_claim_after_compact_continuation_marker_expires(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "expired")
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)
    stale_time = datetime.now(UTC) - timedelta(seconds=COMPACT_SELF_CONTINUE_FRESH_SECONDS + 1)
    assert mark_compact_self_continuation_pending(
        temp_db,
        session_id,
        now=stale_time,
    )

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed == 1
    assert _claim(temp_db, task.id) is None


def test_sweep_skips_closed_and_escalated_tasks(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    _make_session(temp_db, sample_project, SESS_DEAD, "expired")
    closed = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)
    escalated = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)
    now = datetime.now(UTC).isoformat()
    temp_db.execute("UPDATE tasks SET closed_at = %s WHERE id = %s", (now, closed.id))
    temp_db.execute(
        "UPDATE tasks SET escalated_at = %s, is_escalated = TRUE WHERE id = %s",
        (now, escalated.id),
    )

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, closed.id) == SESS_DEAD
    assert _claim(temp_db, escalated.id) == SESS_DEAD


def test_generic_sweep_defers_live_session_claims(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    _make_session(temp_db, sample_project, SESS_DEAD, "expired")
    task = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)
    LocalTaskManager(temp_db).add_label(task.id, "live-session")

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed == 0
    assert _claim(temp_db, task.id) == SESS_DEAD
