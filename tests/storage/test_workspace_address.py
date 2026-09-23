"""Project workspace addressing: one default per machine and project."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.workspace_address import resolve_launch_workspace
from gobby.storage.workspaces import WorkspaceManager
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def manager(temp_db: HubDatabase) -> WorkspaceManager:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    return WorkspaceManager(temp_db)


def _project(db: HubDatabase, name: str) -> str:
    project_id = str(uuid.uuid4())
    db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_id, name))
    return project_id


def test_game_goblins_after_gobby_resolves_to_0_1_0(
    manager: WorkspaceManager, temp_db: HubDatabase
) -> None:
    gobby = _project(temp_db, "Gobby")
    goblins = _project(temp_db, "Game Goblins")
    gobby_workspace, gobby_created = resolve_launch_workspace(
        manager, LOCAL_MACHINE_ID, workspace=None, project_id=gobby
    )
    assert gobby_created is True
    assert gobby_workspace.default_project_id == gobby
    assert gobby_workspace.ref == 0
    repeated, repeated_created = resolve_launch_workspace(
        manager, LOCAL_MACHINE_ID, workspace=None, project_id=gobby
    )
    assert repeated_created is False
    assert repeated.id == gobby_workspace.id

    goblins_workspace, goblins_created = resolve_launch_workspace(
        manager, LOCAL_MACHINE_ID, workspace=None, project_id=goblins
    )
    assert goblins_created is True
    assert goblins_workspace.default_project_id == goblins
    assert goblins_workspace.ref == 1
    change = manager.create_tab(goblins_workspace.id, pane_id=str(uuid.uuid4()), project_id=goblins)
    tab, pane = change.tabs[0], change.panes[0]
    node = manager.resolve_node()
    assert node.ref == 0
    assert f"{node.ref}:{goblins_workspace.ref}:{tab.ref}" == "0:1:0"
    assert f"{node.ref}:{goblins_workspace.ref}:{tab.ref}:{pane.ref}" == "0:1:0:0"


def test_explicit_workspace_wins_and_may_mix_projects(
    manager: WorkspaceManager, temp_db: HubDatabase
) -> None:
    project_id = _project(temp_db, "Game Goblins")
    scratch, scratch_created = resolve_launch_workspace(
        manager, LOCAL_MACHINE_ID, workspace=None, project_id=None
    )
    assert scratch_created is True
    assert scratch.name == "default"
    assert scratch.default_project_id is None
    chosen, created = resolve_launch_workspace(
        manager, LOCAL_MACHINE_ID, workspace=scratch.name, project_id=project_id
    )
    assert created is False
    assert chosen.id == scratch.id
    assert chosen.default_project_id is None


def test_projectless_scratches_share_a_machine(manager: WorkspaceManager) -> None:
    first, first_created = manager.create(LOCAL_MACHINE_ID, "alpha")
    second, second_created = manager.create(LOCAL_MACHINE_ID, "beta")
    assert first_created is True
    assert second_created is True
    assert first.default_project_id is None
    assert second.default_project_id is None
    assert first.id != second.id


def test_worktree_stays_off_the_address(manager: WorkspaceManager, temp_db: HubDatabase) -> None:
    project_id = _project(temp_db, "Game Goblins")
    worktree_id = str(uuid.uuid4())
    temp_db.execute(
        """
        INSERT INTO worktrees (
            id, project_id, machine_id, branch_name, worktree_path, status,
            created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (worktree_id, project_id, LOCAL_MACHINE_ID, "feature", "/tmp/game-goblins", "active"),
    )
    workspace, _created = resolve_launch_workspace(
        manager, LOCAL_MACHINE_ID, workspace=None, project_id=project_id
    )
    change = manager.create_tab(
        workspace.id,
        pane_id=str(uuid.uuid4()),
        project_id=project_id,
        worktree_id=worktree_id,
    )
    tab, pane = change.tabs[0], change.panes[0]
    node = manager.resolve_node()
    address = f"{node.ref}:{workspace.ref}:{tab.ref}:{pane.ref}"
    assert tab.worktree_id == worktree_id
    assert worktree_id not in address
    assert address.count(":") == 3


def test_concurrent_resolution_creates_one_project_workspace(
    manager: WorkspaceManager, temp_db: HubDatabase
) -> None:
    project_id = _project(temp_db, "Game Goblins")
    workers = 4
    barrier = threading.Barrier(workers)
    found: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def resolve() -> None:
        local = WorkspaceManager(temp_db)
        try:
            barrier.wait(timeout=5)
            workspace, _created = resolve_launch_workspace(
                local, LOCAL_MACHINE_ID, workspace=None, project_id=project_id
            )
            with lock:
                found.append(workspace.id)
        except BaseException as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(found)) == 1
    rows = temp_db.fetchall(
        "SELECT id FROM workspaces WHERE machine_id = %s AND default_project_id = %s",
        (LOCAL_MACHINE_ID, project_id),
    )
    assert len(rows) == 1


def test_snapshot_row_carries_the_association(
    manager: WorkspaceManager, temp_db: HubDatabase
) -> None:
    project_id = _project(temp_db, "Game Goblins")
    workspace, _created = resolve_launch_workspace(
        manager, LOCAL_MACHINE_ID, workspace=None, project_id=project_id
    )
    assert workspace.to_dict()["default_project_id"] == project_id
