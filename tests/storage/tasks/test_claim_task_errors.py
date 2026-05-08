"""Focused storage claim error coverage."""

from __future__ import annotations

import pytest

from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import (
    LocalTaskManager,
    TaskAlreadyClaimedError,
    TaskClosedError,
)
from gobby.storage.tasks._transitions import claim_task, close_task

pytestmark = pytest.mark.unit


def test_claim_task_raises_task_closed_error(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Closed task",
    )
    close_task(temp_db, task.id, reason="test")

    with pytest.raises(TaskClosedError, match="task is closed"):
        claim_task(temp_db, task.id, "session-1")


def test_claim_task_raises_task_already_claimed_error(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Claimed task",
    )
    session_manager = SessionManager(temp_db)
    owner = session_manager.register(
        external_id="owner-session",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    other = session_manager.register(
        external_id="other-session",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    claim_task(temp_db, task.id, owner.id)

    with pytest.raises(TaskAlreadyClaimedError) as error:
        claim_task(temp_db, task.id, other.id)

    assert error.value.task_id == task.id
    assert error.value.claimed_by == owner.id
