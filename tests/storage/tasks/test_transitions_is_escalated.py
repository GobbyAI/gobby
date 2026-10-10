"""Phase 5 first-class escalation transition contracts."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_escalate_round_trip(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Escalate me",
        validation_criteria="Test task completion is observable.",
    )

    escalated = manager.escalate_task(task.id, reason="blocked by operator")
    row = temp_db.fetchone(
        "SELECT is_escalated, escalated_at, escalation_reason FROM tasks WHERE id = %s",
        (task.id,),
    )
    assert row is not None
    assert row["is_escalated"] is True
    assert row["escalated_at"] is not None
    assert row["escalation_reason"] == "blocked by operator"
    assert escalated.is_escalated is True

    de_escalated = manager.de_escalate_task(task.id, reason="operator cleared")
    row = temp_db.fetchone(
        "SELECT is_escalated, escalated_at, escalation_reason FROM tasks WHERE id = %s",
        (task.id,),
    )
    assert row is not None
    assert row["is_escalated"] is False
    assert row["escalated_at"] is None
    assert row["escalation_reason"] is None
    assert de_escalated.is_escalated is False


def test_de_escalate_preserves_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    manager = LocalTaskManager(temp_db)
    session = session_manager.register(
        external_id="de-escalate-owner-ext",
        machine_id="21000000-0000-4000-8000-000000000002",
        source="codex",
        project_id=sample_project["id"],
    )
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Escalate me",
        validation_criteria="Test task completion is observable.",
    )

    manager.escalate_task(task.id, reason="blocked by operator")
    manager.claim_task(task.id, session.id)

    de_escalated = manager.de_escalate_task(task.id, reason="operator cleared")
    row = temp_db.fetchone(
        "SELECT claimed_by_session_id FROM tasks WHERE id = %s",
        (task.id,),
    )
    assert row is not None
    assert row["claimed_by_session_id"] == session.id
    assert de_escalated.claimed_by_session_id == session.id
    assert de_escalated.is_escalated is False
