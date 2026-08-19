"""End-to-end storage contract for daemon-local worktrees and clones."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

import gobby.storage.clones as clones_module
import gobby.storage.workspace_machine_scope as scope_module
import gobby.storage.worktrees as worktrees_module
from gobby.storage.clones import CloneStatus, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.storage.worktrees import LocalWorktreeManager, WorktreeStatus
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.integration


def _insert_machine(db: HubDatabase, machine_id: str) -> None:
    db.execute(
        "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s)",
        (machine_id, f"host-{machine_id}", TEST_USER_ID),
    )


def _insert_session(db: HubDatabase, project_id: str, machine_id: str) -> str:
    session_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, %s, 'codex', %s)
        """,
        (session_id, f"workspace-scope-{session_id}", machine_id, project_id),
    )
    return session_id


def _set_local_machine(monkeypatch: pytest.MonkeyPatch, machine_id: str) -> None:
    monkeypatch.setattr(scope_module, "require_machine_id", lambda: machine_id)
    monkeypatch.setattr(worktrees_module, "require_machine_id", lambda: machine_id)
    monkeypatch.setattr(clones_module, "require_machine_id", lambda: machine_id)


def _assert_mismatch(exc: MachineOwnershipMismatchError, *, owner: str, current: str) -> None:
    payload = exc.to_dict()
    assert payload["success"] is False
    assert payload["error_code"] == "machine_ownership_mismatch"
    assert payload["owner_machine_id"] == owner
    assert payload["current_machine_id"] == current


def test_every_workspace_lifecycle_surface_is_machine_scoped(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_machine_id = str(uuid.uuid4())
    foreign_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, foreign_machine_id):
        _insert_machine(temp_db, machine_id)
    _set_local_machine(monkeypatch, local_machine_id)

    project_id = sample_project["id"]
    local_session_id = _insert_session(temp_db, project_id, local_machine_id)
    foreign_session_id = _insert_session(temp_db, project_id, foreign_machine_id)
    stale_at = datetime.now(UTC) - timedelta(hours=48)

    worktrees = LocalWorktreeManager(temp_db)
    local_worktree = worktrees.create(
        project_id=project_id,
        branch_name="task/shared-worktree",
        worktree_path="/tmp/shared-worktree",
        agent_session_id=local_session_id,
    )
    foreign_worktree_id = str(uuid.uuid4())
    temp_db.execute(
        """
        INSERT INTO worktrees (
            id, project_id, machine_id, branch_name, worktree_path, base_branch,
            status, merge_state, last_activity_at
        ) VALUES (%s, %s, %s, %s, %s, 'main', %s, 'pending', %s)
        """,
        (
            foreign_worktree_id,
            project_id,
            foreign_machine_id,
            "task/shared-worktree",
            "/tmp/shared-worktree",
            WorktreeStatus.ACTIVE.value,
            stale_at,
        ),
    )
    worktrees.release(local_worktree.id)
    worktrees.touch(local_worktree.id)
    worktrees.set_merge_state(local_worktree.id, "pending")

    assert [item.id for item in worktrees.list_worktrees()] == [local_worktree.id]
    assert worktrees.count_by_status(project_id) == {WorktreeStatus.ACTIVE.value: 1}
    assert [item.id for item in worktrees.get_by_merge_state("pending")] == [local_worktree.id]
    assert worktrees.find_stale(project_id, hours=24) == []

    worktree_operations: tuple[Callable[[], object], ...] = (
        lambda: worktrees.get(foreign_worktree_id),
        lambda: worktrees.update(foreign_worktree_id, status=WorktreeStatus.STALE.value),
        lambda: worktrees.touch(foreign_worktree_id),
        lambda: worktrees.claim(foreign_worktree_id, local_session_id),
        lambda: worktrees.is_claimed_by_live_session(foreign_worktree_id),
        lambda: worktrees.release(foreign_worktree_id),
        lambda: worktrees.mark_stale(foreign_worktree_id),
        lambda: worktrees.mark_merged(foreign_worktree_id),
        lambda: worktrees.mark_abandoned(foreign_worktree_id),
        lambda: worktrees.set_merge_state(foreign_worktree_id, "resolved"),
        lambda: worktrees.delete(foreign_worktree_id),
    )
    for operation in worktree_operations:
        with pytest.raises(MachineOwnershipMismatchError) as error:
            operation()
        _assert_mismatch(error.value, owner=foreign_machine_id, current=local_machine_id)

    clones = LocalCloneManager(temp_db)
    local_clone = clones.create(
        project_id=project_id,
        branch_name="task/shared-clone",
        clone_path="/tmp/shared-clone",
        agent_session_id=local_session_id,
    )
    foreign_clone_id = str(uuid.uuid4())
    temp_db.execute(
        """
        INSERT INTO clones (
            id, project_id, machine_id, branch_name, clone_path, base_branch,
            status, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'main', %s, %s)
        """,
        (
            foreign_clone_id,
            project_id,
            foreign_machine_id,
            "task/shared-clone",
            "/tmp/shared-clone",
            CloneStatus.ACTIVE.value,
            stale_at,
        ),
    )
    clones.release(local_clone.id)
    clones.record_sync(local_clone.id)

    assert [item.id for item in clones.list_clones()] == [local_clone.id]
    assert clones.count_by_status(project_id) == {CloneStatus.ACTIVE.value: 1}
    assert clones.find_stale(project_id, hours=24) == []

    clone_operations: tuple[Callable[[], object], ...] = (
        lambda: clones.get(foreign_clone_id),
        lambda: clones.update(foreign_clone_id, status=CloneStatus.STALE.value),
        lambda: clones.claim(foreign_clone_id, local_session_id),
        lambda: clones.release(foreign_clone_id),
        lambda: clones.mark_syncing(foreign_clone_id),
        lambda: clones.mark_stale(foreign_clone_id),
        lambda: clones.mark_cleanup(foreign_clone_id),
        lambda: clones.mark_merged(foreign_clone_id),
        lambda: clones.record_sync(foreign_clone_id),
        lambda: clones.delete(foreign_clone_id),
    )
    for operation in clone_operations:
        with pytest.raises(MachineOwnershipMismatchError) as error:
            operation()
        _assert_mismatch(error.value, owner=foreign_machine_id, current=local_machine_id)

    assert worktrees.get_by_path("/tmp/shared-worktree") is not None
    assert clones.get_by_path("/tmp/shared-clone") is not None
    assert foreign_session_id != local_session_id


def test_workspace_session_binding_requires_same_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_machine_id = str(uuid.uuid4())
    foreign_machine_id = str(uuid.uuid4())
    for machine_id in (local_machine_id, foreign_machine_id):
        _insert_machine(temp_db, machine_id)
    _set_local_machine(monkeypatch, local_machine_id)

    project_id = sample_project["id"]
    local_session_id = _insert_session(temp_db, project_id, local_machine_id)
    foreign_session_id = _insert_session(temp_db, project_id, foreign_machine_id)
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)

    with pytest.raises(MachineOwnershipMismatchError) as worktree_error:
        worktrees.create(
            project_id=project_id,
            branch_name="task/foreign-session-worktree",
            worktree_path="/tmp/foreign-session-worktree",
            agent_session_id=foreign_session_id,
        )
    assert worktree_error.value.resource_kind == "session"

    with pytest.raises(MachineOwnershipMismatchError) as clone_error:
        clones.create(
            project_id=project_id,
            branch_name="task/foreign-session-clone",
            clone_path="/tmp/foreign-session-clone",
            agent_session_id=foreign_session_id,
        )
    assert clone_error.value.resource_kind == "session"

    worktree = worktrees.create(
        project_id=project_id,
        branch_name="task/local-session-worktree",
        worktree_path="/tmp/local-session-worktree",
    )
    clone = clones.create(
        project_id=project_id,
        branch_name="task/local-session-clone",
        clone_path="/tmp/local-session-clone",
    )
    with pytest.raises(MachineOwnershipMismatchError):
        worktrees.claim(worktree.id, foreign_session_id)
    with pytest.raises(MachineOwnershipMismatchError):
        clones.claim(clone.id, foreign_session_id)

    with pytest.raises(psycopg.IntegrityError):
        temp_db.execute(
            "UPDATE worktrees SET agent_session_id = %s WHERE id = %s",
            (foreign_session_id, worktree.id),
        )
    with pytest.raises(psycopg.IntegrityError):
        temp_db.execute(
            "UPDATE clones SET agent_session_id = %s WHERE id = %s",
            (foreign_session_id, clone.id),
        )

    assert worktrees.claim(worktree.id, local_session_id) is not None
    assert clones.claim(clone.id, local_session_id) is not None
