"""Machine-scoped project list and source-control status (#24602, hub-mode 4.7)."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import gobby.servers.routes.projects as projects_routes
import gobby.servers.routes.source_control as source_control_routes
import gobby.servers.routes.source_control_git as source_control_git
from gobby.hub.node_client import NodeCommandClient, NodeCommandError
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import OTHER_USER_ID, authenticate_as

pytestmark = pytest.mark.unit

NULL_GIT: dict[str, Any] = {
    "current_branch": None,
    "branch_count": 0,
    "ahead": None,
    "behind": None,
}
NODE_GIT: dict[str, Any] = {"current_branch": "main", "branch_count": 3, "ahead": 2, "behind": 1}
STATUS_TIMEOUT_MS = 15_000


class FakeNodeClient(NodeCommandClient):
    """Node channel double that records checkout.status sends and exposes a settable fleet."""

    def __init__(self) -> None:
        self.connected: set[str] = set()
        self.read_error: NodeCommandError | None = None
        self.reply_error: NodeCommandError | None = None
        self.sent: list[tuple[str, str, dict[str, Any], int]] = []

    async def connected_machine_ids(self) -> set[str]:
        if self.read_error is not None:
            raise self.read_error
        return set(self.connected)

    async def send(
        self, machine_id: str, command: str, payload: dict[str, Any], timeout_ms: int = 30000
    ) -> dict[str, Any]:
        self.sent.append((machine_id, command, payload, timeout_ms))
        if self.reply_error is not None:
            raise self.reply_error
        return dict(NODE_GIT)


@dataclass(frozen=True)
class Fleet:
    hub: str
    node: str
    hub_project_id: str
    node_project_id: str
    node_root: str


@pytest.fixture(autouse=True)
def _clear_status_cache() -> Iterator[None]:
    source_control_git._cache.clear()
    yield
    source_control_git._cache.clear()


@pytest.fixture
def fake_node(monkeypatch: pytest.MonkeyPatch) -> FakeNodeClient:
    fake = FakeNodeClient()
    monkeypatch.setattr(source_control_routes, "node_command_client", lambda: fake)
    return fake


@pytest.fixture
def fleet(temp_db: HubDatabase, sample_project: dict[str, Any]) -> Fleet:
    """A hub with the sample checkout and a node holding a checkout of its own project."""
    hub = require_machine_id()
    node = str(uuid4())
    machines = LocalMachineManager(temp_db)
    machines.upsert_seen(hub, TEST_USER_ID)
    machines.upsert_seen(node, TEST_USER_ID, label="node")
    node_project = LocalProjectManager(temp_db).create(name=f"node-only-{uuid4().hex[:8]}")
    node_root = f"/node-only/{node}/checkout"
    LocalProjectCheckoutManager(temp_db).register(node, node_project.id, node_root)
    for _ in range(2):
        worktree = LocalWorktreeManager(temp_db).create(
            project_id=node_project.id,
            branch_name=f"feature/{uuid4().hex}",
            worktree_path=f"/node-only/{uuid4()}/worktree",
        )
        temp_db.execute("UPDATE worktrees SET machine_id = %s WHERE id = %s", (node, worktree.id))
    clone = LocalCloneManager(temp_db).create(
        project_id=node_project.id,
        branch_name=f"feature/{uuid4().hex}",
        clone_path=f"/node-only/{uuid4()}/clone",
    )
    temp_db.execute("UPDATE clones SET machine_id = %s WHERE id = %s", (node, clone.id))
    return Fleet(
        hub=hub,
        node=node,
        hub_project_id=sample_project["id"],
        node_project_id=node_project.id,
        node_root=node_root,
    )


@pytest.fixture
def hub_root(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    """The hub's sample checkout as a git repository, so the local status path can run."""
    checkout = LocalProjectCheckoutManager(temp_db).get(require_machine_id(), sample_project["id"])
    assert checkout is not None
    subprocess.run(["git", "init", "-q", checkout.root_path], check=True)
    return checkout.root_path


def _status(client: TestClient, **params: str) -> Any:
    return client.get("/api/source-control/status", params=params)


def _node_status_body(fleet: Fleet, git: dict[str, Any]) -> dict[str, Any]:
    return {
        **git,
        "worktree_count": 2,
        "clone_count": 1,
        "repo_path": fleet.node_root,
    }


def test_projects_for_node_machine(
    client: TestClient,
    http_server: Any,
    fleet: Fleet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticate_as(http_server, TEST_USER_ID)

    def read_checkout_path(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a node checkout's root must not be read on the hub")

    monkeypatch.setattr(projects_routes, "load_project_approval_rules", read_checkout_path)
    monkeypatch.setattr(projects_routes, "load_project_validation_detection", read_checkout_path)

    response = client.get("/api/projects", params={"machine_id": fleet.node})

    assert response.status_code == 200
    rows = response.json()
    assert [row["id"] for row in rows] == [fleet.node_project_id]
    assert rows[0]["checkout"] == {"machine_id": fleet.node, "root_path": fleet.node_root}
    assert rows[0]["approval_rules"] == []
    assert rows[0]["validation_detection"] is None


def test_remote_status_cached(
    client: TestClient,
    http_server: Any,
    fleet: Fleet,
    fake_node: FakeNodeClient,
) -> None:
    authenticate_as(http_server, TEST_USER_ID)
    fake_node.connected = {fleet.node}

    first = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    second = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)

    assert first.status_code == 200
    assert first.json() == _node_status_body(fleet, NODE_GIT)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert fake_node.sent == [
        (
            fleet.node,
            "checkout.status",
            {"project_id": fleet.node_project_id, "root_path": fleet.node_root},
            STATUS_TIMEOUT_MS,
        )
    ]


def test_offline_node_shows_no_stale_state(
    client: TestClient,
    http_server: Any,
    fleet: Fleet,
    fake_node: FakeNodeClient,
) -> None:
    authenticate_as(http_server, TEST_USER_ID)
    null_body = _node_status_body(fleet, NULL_GIT)

    offline = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    assert offline.status_code == 200
    assert offline.json() == null_body
    assert fake_node.sent == []

    fake_node.connected = {fleet.node}
    online = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    assert online.json() == _node_status_body(fleet, NODE_GIT)
    assert len(fake_node.sent) == 1

    fake_node.connected = set()
    gone = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    assert gone.status_code == 200
    assert gone.json() == null_body
    assert len(fake_node.sent) == 1

    fake_node.connected = {fleet.node}
    fake_node.read_error = NodeCommandError(
        "node_routing_unavailable", "Cannot read connected nodes", "none"
    )
    unreadable = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    assert unreadable.status_code == 200
    assert unreadable.json() == null_body
    assert len(fake_node.sent) == 1

    fake_node.read_error = None
    back = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    assert back.json() == _node_status_body(fleet, NODE_GIT)
    assert len(fake_node.sent) == 2


def test_checkout_status_error_answers_null_and_is_not_cached(
    client: TestClient,
    http_server: Any,
    fleet: Fleet,
    fake_node: FakeNodeClient,
) -> None:
    authenticate_as(http_server, TEST_USER_ID)
    fake_node.connected = {fleet.node}
    fake_node.reply_error = NodeCommandError("node_reply_lost", "Node reply lost", None)

    failed = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    assert failed.status_code == 200
    assert failed.json() == _node_status_body(fleet, NULL_GIT)

    fake_node.reply_error = None
    retried = _status(client, machine_id=fleet.node, project_id=fleet.node_project_id)
    assert retried.json() == _node_status_body(fleet, NODE_GIT)
    assert len(fake_node.sent) == 2


def test_local_routes_unchanged(
    client: TestClient,
    http_server: Any,
    fleet: Fleet,
    fake_node: FakeNodeClient,
    hub_root: str,
) -> None:
    authenticate_as(http_server, TEST_USER_ID)
    local_status = {
        "current_branch": None,
        "branch_count": 0,
        "worktree_count": 0,
        "clone_count": 0,
        "repo_path": hub_root,
        "ahead": None,
        "behind": None,
    }

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    rows = {row["id"]: row for row in listed.json()}
    assert rows[fleet.hub_project_id]["checkout"] == {
        "machine_id": fleet.hub,
        "root_path": hub_root,
    }
    assert rows[fleet.node_project_id]["checkout"] is None

    omitted = _status(client, project_id=fleet.hub_project_id)
    hub_named = _status(client, machine_id=fleet.hub, project_id=fleet.hub_project_id)
    assert omitted.status_code == 200
    assert omitted.json() == local_status
    assert hub_named.json() == local_status
    assert fake_node.sent == []


def test_routes_need_owned_machine(
    client: TestClient,
    http_server: Any,
    fleet: Fleet,
    fake_node: FakeNodeClient,
    hub_root: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []
    real_get = LocalProjectCheckoutManager.get
    real_list = LocalProjectCheckoutManager.list_for_machine
    real_resolve = source_control_routes._resolve_project
    real_responses = projects_routes._projects_to_responses

    def spy_get(self: LocalProjectCheckoutManager, machine_id: str, project_id: str) -> Any:
        touched.append("checkout.get")
        return real_get(self, machine_id, project_id)

    def spy_list(self: LocalProjectCheckoutManager, machine_id: str) -> Any:
        touched.append("checkout.list_for_machine")
        return real_list(self, machine_id)

    def spy_resolve(*args: Any, **kwargs: Any) -> Any:
        touched.append("resolve_project")
        return real_resolve(*args, **kwargs)

    def spy_responses(*args: Any, **kwargs: Any) -> Any:
        touched.append("project_responses")
        return real_responses(*args, **kwargs)

    monkeypatch.setattr(LocalProjectCheckoutManager, "get", spy_get)
    monkeypatch.setattr(LocalProjectCheckoutManager, "list_for_machine", spy_list)
    monkeypatch.setattr(source_control_routes, "_resolve_project", spy_resolve)
    monkeypatch.setattr(projects_routes, "_projects_to_responses", spy_responses)

    stale = {"current_branch": "stale", "branch_count": 9, "ahead": 0, "behind": 0}
    cache_key = f"remote-status:{fleet.node}:{fleet.node_project_id}"
    source_control_git._set_cached(cache_key, stale)
    fake_node.connected = {fleet.node}

    list_machines: list[dict[str, str]] = [
        {},
        {"machine_id": fleet.hub},
        {"machine_id": fleet.node},
    ]
    status_cases: list[tuple[dict[str, str], str]] = [
        ({}, fleet.hub_project_id),
        ({"machine_id": fleet.hub}, fleet.hub_project_id),
        ({"machine_id": fleet.node}, fleet.node_project_id),
    ]

    authenticate_as(http_server, OTHER_USER_ID)
    for machines in list_machines:
        refused = client.get("/api/projects", params=machines)
        assert refused.status_code == 403
        assert refused.json()["error_code"] == "machine_not_owned"
    for machines, project_id in status_cases:
        refused = _status(client, project_id=project_id, **machines)
        assert refused.status_code == 403
        assert refused.json()["error_code"] == "machine_not_owned"
        assert "current_branch" not in refused.json()

    assert touched == []
    assert fake_node.sent == []
    assert source_control_git._get_cached(cache_key, 30.0) == stale

    authenticate_as(http_server, TEST_USER_ID)
    for machines in list_machines:
        owned = client.get("/api/projects", params=machines)
        assert owned.status_code == 200
    for machines, project_id in status_cases:
        owned = _status(client, project_id=project_id, **machines)
        assert owned.status_code == 200
    owned_node = _status(client, project_id=fleet.node_project_id, machine_id=fleet.node)
    assert owned_node.json() == _node_status_body(fleet, stale)
    assert fake_node.sent == []
