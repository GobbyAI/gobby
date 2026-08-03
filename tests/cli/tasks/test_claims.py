from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.cli.tasks._utils.claims import get_claimed_task_owners
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_claimed_task_owners_reads_tasks_claimed_by_active_sessions(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    active_owner = session_manager.register(
        external_id="active-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="cli",
        project_id=sample_project["id"],
    )
    expired_owner = session_manager.register(
        external_id="expired-owner",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="cli",
        project_id=sample_project["id"],
    )
    session_manager.update_status(expired_owner.id, "expired")
    active_task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Active claim",
        validation_criteria="Active canonical claims appear in CLI task listings.",
        claimed_by_session_id=active_owner.id,
    )
    task_manager.create_task(
        project_id=sample_project["id"],
        title="Expired claim",
        validation_criteria="Expired canonical claims stay out of CLI task listings.",
        claimed_by_session_id=expired_owner.id,
    )

    assert get_claimed_task_owners(temp_db) == {active_task.id: active_owner.id}


def test_claimed_task_owners_degrades_on_psycopg_failure() -> None:
    database = MagicMock()
    database.fetchall.side_effect = psycopg.OperationalError("database unavailable")

    assert get_claimed_task_owners(database) == {}
