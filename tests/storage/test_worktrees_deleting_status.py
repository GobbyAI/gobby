"""A worktree record marked deleting is neither handed out nor read as the task's tree (#25838)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager
from tests.fixtures.isolated_checkout import (
    install_isolated_checkout_project,
    patch_local_machine_id,
)

pytestmark = pytest.mark.integration

MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, MACHINE_ID)


@pytest.fixture
def project_id(temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    isolated = install_isolated_checkout_project(
        temp_db,
        tmp_path / "isolated-checkout",
        machine_id=MACHINE_ID,
        monkeypatch=monkeypatch,
    )
    return str(isolated.project.to_dict()["id"])


def test_claim_refuses_a_worktree_being_deleted(
    temp_db: HubDatabase,
    project_id: str,
    session_manager: SessionManager,
) -> None:
    manager = LocalWorktreeManager(temp_db)
    session = session_manager.register(
        external_id=f"deleting-claim-{uuid.uuid4()}",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project_id,
    )
    row = manager.create(
        project_id=project_id,
        branch_name="task/deleting-claim",
        worktree_path="/deleting/claim",
    )
    manager.update(row.id, status="deleting")

    assert manager.claim_if_available(row.id, session.id) is None
    unclaimed = manager.get(row.id)
    assert unclaimed is not None
    assert unclaimed.agent_session_id is None


def test_get_by_task_hides_a_worktree_being_deleted(
    temp_db: HubDatabase,
    project_id: str,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        project_id=project_id,
        title="Worktree being deleted",
        validation_criteria="Storage fixture task; behavior asserted by the test.",
    )
    manager = LocalWorktreeManager(temp_db)
    row = manager.create(
        project_id=project_id,
        branch_name="feature/deleting",
        worktree_path="/tmp/gobby-deleting",
        task_id=task.id,
    )
    manager.update(row.id, status="deleting")

    assert manager.get_by_task(task.id) is None
