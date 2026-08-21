"""Focused storage claim error coverage."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import (
    LocalTaskManager,
    TaskAlreadyClaimedError,
    TaskClosedError,
)
from gobby.storage.tasks._transitions import claim_task, close_task

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_claim_task_raises_task_closed_error(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Closed task",
        validation_criteria="Test task completion is observable.",
    )
    close_task(temp_db, task.id, reason="test")

    with pytest.raises(TaskClosedError, match="task is closed"):
        claim_task(temp_db, task.id, "session-1")


def test_claim_task_raises_task_already_claimed_error(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Claimed task",
        validation_criteria="Test task completion is observable.",
    )
    session_manager = SessionManager(temp_db)
    owner = session_manager.register(
        external_id="owner-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    other = session_manager.register(
        external_id="other-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    claim_task(temp_db, task.id, owner.id)

    with pytest.raises(TaskAlreadyClaimedError) as error:
        claim_task(temp_db, task.id, other.id)

    assert error.value.task_id == task.id
    assert error.value.claimed_by == owner.id


def test_concurrent_claim_has_exactly_one_winner(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Contended task",
        validation_criteria="Test task completion is observable.",
    )
    session_manager = SessionManager(temp_db)
    claimants = [
        session_manager.register(
            external_id=f"claimant-{index}",
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        for index in range(4)
    ]
    barrier = threading.Barrier(len(claimants))
    winners: list[str] = []
    conflicts: list[TaskAlreadyClaimedError] = []
    unexpected: list[BaseException] = []
    lock = threading.Lock()

    def _claim(session_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            claimed = claim_task(temp_db, task.id, session_id)
            with lock:
                winners.append(claimed.claimed_by_session_id or "")
        except TaskAlreadyClaimedError as exc:
            with lock:
                conflicts.append(exc)
        except BaseException as exc:  # pragma: no cover - asserted below
            with lock:
                unexpected.append(exc)

    threads = [threading.Thread(target=_claim, args=(claimant.id,)) for claimant in claimants]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert unexpected == []
    assert len(winners) == 1
    assert len(conflicts) == len(claimants) - 1
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id == winners[0]


def test_expected_owner_does_not_stomp_replacement_owner(temp_db, sample_project) -> None:
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Delegated task",
        validation_criteria="Test task completion is observable.",
    )
    session_manager = SessionManager(temp_db)
    owner = session_manager.register(
        external_id="delegating-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    replacement = session_manager.register(
        external_id="replacement-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id="delegated-child",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    claim_task(temp_db, task.id, owner.id)
    claim_task(temp_db, task.id, replacement.id, force=True)

    with pytest.raises(TaskAlreadyClaimedError) as error:
        claim_task(temp_db, task.id, child.id, expected_owner=owner.id)

    assert error.value.claimed_by == replacement.id
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id == replacement.id


def test_expected_owner_transfers_when_current_owner_matches(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Predecessor-owned task",
        validation_criteria="Test task completion is observable.",
    )
    session_manager = SessionManager(temp_db)
    predecessor = session_manager.register(
        external_id="predecessor-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    successor = session_manager.register(
        external_id="successor-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    claim_task(temp_db, task.id, predecessor.id)

    claimed = claim_task(
        temp_db,
        task.id,
        successor.id,
        expected_owner=predecessor.id,
    )

    assert claimed.claimed_by_session_id == successor.id
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id == successor.id
