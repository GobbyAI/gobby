"""Remote expiry delegates deletion safety to the owning node."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from gobby.hub import remote_workspaces
from gobby.hub.node_client import NodeCommandError
from gobby.hub.remote_workspaces import Workspace, WorkspaceKind
from gobby.runner_maintenance import isolation
from gobby.storage.clones import Clone, LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


@pytest.fixture
def node_machine(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    machines = LocalMachineManager(temp_db)
    machines.upsert_seen(require_machine_id(), TEST_USER_ID)
    node = str(uuid4())
    machines.upsert_seen(node, TEST_USER_ID)
    temp_db.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) VALUES (%s, %s, %s)",
        (node, sample_project["id"], "/node/checkout"),
    )
    return node


def _workspace(
    db: HubDatabase, project: str, machine: str, kind: WorkspaceKind, *, session: bool = False
) -> Workspace:
    task = LocalTaskManager(db).create_task(
        project, "Remote expiry owner", validation_criteria="Node confirms safe removal"
    )
    db.execute("UPDATE tasks SET closed_at = NOW() WHERE id = %s", (task.id,))
    storage = LocalWorktreeManager(db) if kind == "worktree" else LocalCloneManager(db)
    row = storage.create(project, f"task/{uuid4()}", f"/node/{uuid4()}", task_id=task.id)
    storage.update(row.id, status="merged", cleanup_after=datetime.now(UTC) - timedelta(days=1))
    table = "worktrees" if kind == "worktree" else "clones"
    db.execute(f"UPDATE {table} SET machine_id = %s WHERE id = %s", (machine, row.id))
    if session:
        session_id = str(uuid4())
        db.execute(
            "INSERT INTO sessions(id, external_id, machine_id, source, project_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session_id, session_id, machine, "codex", project),
        )
        db.execute(f"UPDATE {table} SET agent_session_id = %s WHERE id = %s", (session_id, row.id))
    result = remote_workspaces.get_remote(db, kind, row.id)
    assert result is not None
    return result


def _path(row: Workspace) -> str:
    return row.worktree_path if isinstance(row, Worktree) else row.clone_path


async def _run_db(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize("session", [False, True])
async def test_expired_node_rows_removed_through_node(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: WorkspaceKind,
    session: bool,
) -> None:
    row = _workspace(temp_db, sample_project["id"], node_machine, kind, session=session)
    client = AsyncMock()
    client.send.return_value = {"outcome": "removed"}
    monkeypatch.setattr(remote_workspaces, "node_command_client", lambda: client)
    runner = AsyncMock(side_effect=_run_db)

    await remote_workspaces.expire_remote_workspaces(temp_db, run_db=runner)

    assert runner.await_count >= 1
    client.send.assert_awaited_once()
    machine, command, payload = client.send.await_args.args
    assert (machine, command) == (node_machine, "workspace.remove")
    assert payload == {
        "project_id": sample_project["id"],
        "mode": kind,
        "path": _path(row),
        "branch_name": row.branch_name,
        "source_root": "/node/checkout",
        "force": False,
        "force_delete_branch": False,
        "merged_into": None,
    }
    assert remote_workspaces.get_remote(temp_db, kind, row.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize("outcome", ["busy", "dirty", "unmerged", "offline"])
async def test_expiry_keeps_rows_the_node_refuses(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: WorkspaceKind,
    outcome: str,
) -> None:
    row = _workspace(temp_db, sample_project["id"], node_machine, kind, session=True)
    excluded_rows: list[Workspace] = []
    table = "worktrees" if kind == "worktree" else "clones"
    for excluded in ("unexpired", "unmerged", "open_task", "claimed_task", "local"):
        machine = require_machine_id() if excluded == "local" else node_machine
        other = _workspace(temp_db, sample_project["id"], machine, kind, session=True)
        excluded_rows.append(other)
        if excluded == "unexpired":
            temp_db.execute(
                f"UPDATE {table} SET cleanup_after = %s WHERE id = %s",
                (datetime.now(UTC) + timedelta(days=1), other.id),
            )
        elif excluded == "unmerged":
            temp_db.execute(f"UPDATE {table} SET status = 'active' WHERE id = %s", (other.id,))
        elif excluded == "open_task":
            temp_db.execute("UPDATE tasks SET closed_at = NULL WHERE id = %s", (other.task_id,))
        elif excluded == "claimed_task":
            temp_db.execute(
                "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s",
                (other.agent_session_id, other.task_id),
            )
    client = AsyncMock()
    if outcome == "offline":
        client.send.side_effect = NodeCommandError("node_offline")
    else:
        client.send.return_value = {"outcome": outcome}
    monkeypatch.setattr(remote_workspaces, "node_command_client", lambda: client)

    await remote_workspaces.expire_remote_workspaces(temp_db, run_db=_run_db)
    await remote_workspaces.expire_remote_workspaces(temp_db, run_db=_run_db)

    assert client.send.await_count == 2
    assert remote_workspaces.get_remote(temp_db, kind, row.id) == row
    for other in excluded_rows:
        assert remote_workspaces.get_remote(temp_db, kind, other.id) is not None
    for call in client.send.await_args_list:
        assert call.args[2]["path"] == _path(row)


@pytest.mark.asyncio
async def test_loop_runs_remote_step(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    def worktrees(self: LocalWorktreeManager) -> list[Worktree]:
        events.append("worktrees")
        return []

    def clones(self: LocalCloneManager) -> list[Clone]:
        events.append("clones")
        return []

    monkeypatch.setattr(LocalWorktreeManager, "find_expired", worktrees)
    monkeypatch.setattr(LocalCloneManager, "find_expired", clones)
    remote = AsyncMock(side_effect=lambda *a, **kw: events.append("remote"))
    monkeypatch.setattr(isolation, "expire_remote_workspaces", remote, raising=False)
    monkeypatch.setattr(isolation, "_cleanup_missing_isolation_records_async", AsyncMock())
    monkeypatch.setattr(isolation, "reconcile_isolation_registry", AsyncMock())
    shutdown = iter([False, False, True])

    await isolation.cleanup_expired_isolation_loop(
        temp_db, lambda: next(shutdown), interval_hours=0, run_db=_run_db
    )

    assert events == ["worktrees", "clones", "remote"] * 2
    assert remote.await_count == 2
    remote.assert_awaited_with(temp_db, run_db=_run_db)
    factory = Mock()
    monkeypatch.setattr(remote_workspaces, "node_command_client", factory)
    await remote_workspaces.expire_remote_workspaces(temp_db, run_db=_run_db)
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["worktree", "clone"])
async def test_row_error_preserves_later_rows_and_loop_steps(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: WorkspaceKind,
) -> None:
    failed = _workspace(temp_db, sample_project["id"], node_machine, kind, session=True)
    later = _workspace(temp_db, sample_project["id"], node_machine, kind, session=True)
    client = AsyncMock()
    client.send.side_effect = [RuntimeError("unexpected removal failure"), {"outcome": "removed"}]
    monkeypatch.setattr(remote_workspaces, "node_command_client", lambda: client)
    missing = AsyncMock()
    reconcile = AsyncMock()
    monkeypatch.setattr(isolation, "_cleanup_missing_isolation_records_async", missing)
    monkeypatch.setattr(isolation, "reconcile_isolation_registry", reconcile)
    shutdown = iter([False, True])

    await isolation.cleanup_expired_isolation_loop(
        temp_db, lambda: next(shutdown), interval_hours=0, run_db=_run_db
    )

    assert client.send.await_count == 2
    assert [call.args[2]["path"] for call in client.send.await_args_list] == [
        _path(failed),
        _path(later),
    ]
    assert remote_workspaces.get_remote(temp_db, kind, failed.id) == failed
    assert remote_workspaces.get_remote(temp_db, kind, later.id) is None
    missing.assert_awaited_once()
    reconcile.assert_awaited_once_with(temp_db, run_db=_run_db)
    assert any(
        failed.id in record.getMessage() and record.exc_info is not None
        for record in caplog.records
    )
