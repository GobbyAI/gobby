"""A seat mid-turn is working, so an old claim with no attestation is not idle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gobby.agents.team_progress import HANDOFF_BOUND_SECONDS, _task_progress
from gobby.sessions.turn_lifecycle import TurnEvidence, TurnLifecycleReducer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def _claimed_past_bound(temp_db: HubDatabase, sample_project: dict[str, Any]) -> tuple[str, Task]:
    sessions = AgentSessions(temp_db, sample_project["id"])
    holder = sessions.add("fixture-worker")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Run mypy in the foreground")
    manager.claim_task(task.id, holder)
    start = datetime.now(UTC) - timedelta(seconds=HANDOFF_BOUND_SECONDS + 300)
    temp_db.execute(
        "UPDATE task_lifecycle_events SET created_at = %s "
        "WHERE task_id = %s AND to_state = 'claim:started'",
        (start, task.id),
    )
    return holder, manager.get_task(task.id)


def test_seat_mid_turn_with_no_attestation_is_not_idle(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    holder, task = _claimed_past_bound(temp_db, sample_project)
    TurnLifecycleReducer(SessionManager(temp_db)).begin_turn(holder, TurnEvidence(source="hook"))

    progress = _task_progress(temp_db, task, datetime.now(UTC))

    assert progress is not None
    assert "idle_on_open_work" not in progress.flags


def test_seat_blocked_on_approval_is_not_masked_as_working(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    holder, task = _claimed_past_bound(temp_db, sample_project)
    lifecycle = TurnLifecycleReducer(SessionManager(temp_db))
    lifecycle.begin_turn(holder, TurnEvidence(source="hook"))
    lifecycle.enter_wait(
        holder, kind="approval", token="approve-1", evidence=TurnEvidence(source="hook")
    )

    progress = _task_progress(temp_db, task, datetime.now(UTC))

    assert progress is not None
    assert "idle_on_open_work" in progress.flags


def test_seat_between_turns_with_no_attestation_is_idle(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    _holder, task = _claimed_past_bound(temp_db, sample_project)

    progress = _task_progress(temp_db, task, datetime.now(UTC))

    assert progress is not None
    assert "idle_on_open_work" in progress.flags
