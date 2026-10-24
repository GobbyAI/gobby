"""Machine authorization and node completion for REST workspace operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gobby.hub import remote_workspaces
from gobby.hub.node_client import DeliveryStage, NodeCommandError
from gobby.servers.http import HTTPServer
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import OTHER_USER_ID, authenticate_as, create_http_server

pytestmark = pytest.mark.unit


@dataclass
class WorkspaceRoutes:
    db: HubDatabase
    server: HTTPServer
    client: TestClient
    node: AsyncMock
    hub_machine: str
    node_machine: str
    project_id: str
    local_ids: dict[str, str]
    remote_ids: dict[str, str]

    def url(self, kind: str) -> str:
        return f"/api/source-control/{kind}s/{self.remote_ids[kind]}"


@pytest.fixture
def routes(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> WorkspaceRoutes:
    hub_machine = require_machine_id()
    node_machine = str(uuid4())
    machines = LocalMachineManager(temp_db)
    for machine in (hub_machine, node_machine):
        machines.upsert_seen(machine, TEST_USER_ID)
    project_id = str(sample_project["id"])
    temp_db.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) VALUES (%s, %s, %s)",
        (node_machine, project_id, "/node-only/checkout"),
    )
    worktrees = LocalWorktreeManager(temp_db)
    clones = LocalCloneManager(temp_db)
    local_ids: dict[str, str] = {}
    remote_ids: dict[str, str] = {}
    for ids, machine in ((local_ids, hub_machine), (remote_ids, node_machine)):
        worktree = worktrees.create(
            project_id=project_id,
            branch_name=f"feature/{uuid4().hex}",
            worktree_path=f"/node-only/{uuid4()}/worktree",
        )
        clone = clones.create(
            project_id=project_id,
            branch_name=f"feature/{uuid4().hex}",
            clone_path=f"/node-only/{uuid4()}/clone",
        )
        ids.update(worktree=worktree.id, clone=clone.id)
        for table, row_id in (("worktrees", worktree.id), ("clones", clone.id)):
            temp_db.execute(
                f"UPDATE {table} SET machine_id = %s WHERE id = %s",  # nosec B608
                (machine, row_id),
            )
    node = AsyncMock()
    node.send.return_value = {"outcome": "removed"}
    monkeypatch.setattr(remote_workspaces, "node_command_client", lambda: node)
    server = create_http_server(database=temp_db, worktree_storage=worktrees, clone_storage=clones)
    authenticate_as(server, TEST_USER_ID)
    return WorkspaceRoutes(
        temp_db,
        server,
        TestClient(server.app),
        node,
        hub_machine,
        node_machine,
        project_id,
        local_ids,
        remote_ids,
    )


@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize("machine", ["node", "hub", "omitted"])
def test_list_routes_take_machine_id(routes: WorkspaceRoutes, kind: str, machine: str) -> None:
    params = {"project_id": routes.project_id}
    if machine != "omitted":
        params["machine_id"] = routes.node_machine if machine == "node" else routes.hub_machine
    response = routes.client.get(f"/api/source-control/{kind}s", params=params)
    assert response.status_code == 200, response.text
    expected = routes.remote_ids if machine == "node" else routes.local_ids
    assert [row["id"] for row in response.json()[f"{kind}s"]] == [expected[kind]]
    filtered = routes.client.get(
        f"/api/source-control/{kind}s", params={**params, "project_id": str(uuid4())}
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()[f"{kind}s"] == []
    if kind == "worktree":
        filtered = routes.client.get(
            "/api/source-control/worktrees", params={**params, "status": "merged"}
        )
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["worktrees"] == []
    routes.node.send.assert_not_awaited()


@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize(
    ("outcome", "code", "status", "stage"),
    [
        ("removed", None, 200, None),
        ("dirty", "dirty", 409, None),
        ("unmerged", "unmerged", 409, None),
        ("busy", "workspace_busy", 409, None),
        ("offline", "node_offline", 503, "none"),
        ("timeout", "node_timeout", 502, "partial"),
    ],
)
def test_delete_routes_finish_through_node(
    routes: WorkspaceRoutes,
    kind: str,
    outcome: str,
    code: str | None,
    status: int,
    stage: DeliveryStage,
) -> None:
    if outcome in {"offline", "timeout"}:
        routes.node.send.side_effect = NodeCommandError(str(code), stage=stage)
    else:
        routes.node.send.return_value = {"outcome": outcome}
    row_id = routes.remote_ids[kind]
    before = routes.db.fetchone(f"SELECT * FROM {kind}s WHERE id = %s", (row_id,))
    assert before is not None
    params = {"merged_into": "main"} if kind == "worktree" else {}
    response = routes.client.delete(routes.url(kind), params=params)
    assert response.status_code == status, response.text
    routes.node.send.assert_awaited_once()
    args = routes.node.send.await_args
    assert args is not None
    path_key = "worktree_path" if kind == "worktree" else "clone_path"
    assert args.args == (
        routes.node_machine,
        "workspace.remove",
        {
            "project_id": routes.project_id,
            "mode": kind,
            "path": before[path_key],
            "branch_name": before["branch_name"],
            "source_root": "/node-only/checkout",
            "force": False,
            "force_delete_branch": False,
            "merged_into": "main" if kind == "worktree" else None,
        },
    )
    after = routes.db.fetchone(f"SELECT * FROM {kind}s WHERE id = %s", (row_id,))
    if status == 200:
        assert response.json() == {"success": True, "id": row_id, "git_deleted": True}
        assert after is None
    else:
        detail = response.json()["detail"]
        assert detail["error_code"] == code
        assert after == before
        if status == 409:
            assert detail["success"] is False
            assert detail["id"] == row_id
        else:
            assert detail["stage"] == stage


def test_sync_route_keeps_ownership_refusal(routes: WorkspaceRoutes) -> None:
    url = routes.url("clone") + "/sync"
    authenticate_as(routes.server, OTHER_USER_ID)
    denied = routes.client.post(url)
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == {"error_code": "machine_not_owned", "stage": "none"}
    authenticate_as(routes.server, TEST_USER_ID)
    response = routes.client.post(url)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error_code"] == "machine_ownership_mismatch"
    routes.node.send.assert_not_awaited()
    assert routes.db.fetchone("SELECT id FROM clones WHERE id = %s", (routes.remote_ids["clone"],))


@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize("operation", ["delete", "list-node", "list-hub", "list-omitted"])
def test_routes_need_owned_machine(
    routes: WorkspaceRoutes, monkeypatch: pytest.MonkeyPatch, kind: str, operation: str
) -> None:
    spies: list[MagicMock] = []
    storage = (
        routes.server.services.worktree_storage
        if kind == "worktree"
        else routes.server.services.clone_storage
    )
    assert storage is not None
    for target, name in (
        (storage, "get"),
        (storage, f"list_{kind}s"),
        (remote_workspaces, "get_remote"),
        (remote_workspaces, "list_remote"),
    ):
        spy = MagicMock(wraps=getattr(target, name))
        monkeypatch.setattr(target, name, spy)
        spies.append(spy)
    authenticate_as(routes.server, OTHER_USER_ID)
    params = {}
    if operation == "list-node":
        params["machine_id"] = routes.node_machine
    elif operation == "list-hub":
        params["machine_id"] = routes.hub_machine
    url = routes.url(kind) if operation == "delete" else f"/api/source-control/{kind}s"
    method = routes.client.delete if operation == "delete" else routes.client.get
    before = routes.db.fetchone(f"SELECT * FROM {kind}s WHERE id = %s", (routes.remote_ids[kind],))
    denied = method(url, params=params)
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"] == {"error_code": "machine_not_owned", "stage": "none"}
    for spy in spies:
        spy.assert_not_called()
    routes.node.send.assert_not_awaited()
    assert (
        routes.db.fetchone(f"SELECT * FROM {kind}s WHERE id = %s", (routes.remote_ids[kind],))
        == before
    )
    authenticate_as(routes.server, TEST_USER_ID)
    allowed = method(url, params=params)
    assert allowed.status_code == 200, allowed.text
    if operation == "delete":
        routes.node.send.assert_awaited_once()
        assert allowed.json()["success"] is True
    else:
        rows = routes.remote_ids if operation == "list-node" else routes.local_ids
        assert [row["id"] for row in allowed.json()[f"{kind}s"]] == [rows[kind]]
