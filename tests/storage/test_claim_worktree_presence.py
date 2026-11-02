"""A claim reports its worktree only while the worktree's registry row exists."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from tests.fixtures.agent_sessions import AgentSessions
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID

pytestmark = pytest.mark.unit

TREE_PATH = "/repo/worktrees/task-24246-team-tree"
MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000002"


def _claimed_with_tree(temp_db: HubDatabase, sample_project: dict[str, Any]) -> tuple[str, str]:
    holder = AgentSessions(temp_db, sample_project["id"]).add("developer")
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Developer tree work")
    manager.claim_task(task.id, holder)
    worktree_id = str(uuid.uuid4())
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=TREE_PATH,
        worktree_id=worktree_id,
        base_commit_sha="abc123",
    )
    return holder, worktree_id


def _insert_registry_row(
    temp_db: HubDatabase, sample_project: dict[str, Any], worktree_id: str
) -> None:
    LocalMachineManager(temp_db).upsert_seen(MACHINE_ID, TEST_USER_ID, hostname="local")
    temp_db.execute(
        "INSERT INTO worktrees (id, project_id, machine_id, branch_name, worktree_path, status,"
        " created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now(), now())",
        (worktree_id, sample_project["id"], MACHINE_ID, "team/24246", TREE_PATH, "active"),
    )


def _claim_worktrees(temp_db: HubDatabase, holder: str) -> list[str | None]:
    claims = SessionTaskManager(temp_db).get_current_claims([holder])
    return [claim["worktree"] for claim in claims[holder]]


def test_child_claim_reports_registered_epic_tree(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    holder, worktree_id = _claimed_with_tree(temp_db, sample_project)
    _insert_registry_row(temp_db, sample_project, worktree_id)
    tasks = LocalTaskManager(temp_db)
    root = SessionTaskManager(temp_db).get_current_claims([holder])[holder][0]
    parent = tasks.resolve_task_reference(root["ref"], sample_project["id"])
    tasks.release_task_claim(parent)
    leaf = tasks.create_task(sample_project["id"], "Epic leaf", parent_task_id=parent)
    tasks.claim_task(leaf.id, holder)
    assert _claim_worktrees(temp_db, holder) == [TREE_PATH]
    temp_db.execute("DELETE FROM worktrees WHERE id = %s", (worktree_id,))
    assert _claim_worktrees(temp_db, holder) == [None]


def test_claim_worktree_reads_present_while_registry_row_exists(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    holder, worktree_id = _claimed_with_tree(temp_db, sample_project)
    _insert_registry_row(temp_db, sample_project, worktree_id)

    assert _claim_worktrees(temp_db, holder) == [TREE_PATH]


def test_claim_worktree_reads_absent_once_registry_row_is_deleted(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    holder, worktree_id = _claimed_with_tree(temp_db, sample_project)
    _insert_registry_row(temp_db, sample_project, worktree_id)
    temp_db.execute("DELETE FROM worktrees WHERE id = %s", (worktree_id,))

    assert _claim_worktrees(temp_db, holder) == [None]
