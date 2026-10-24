"""Hub routing for worktree and clone rows that a node owns."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import gobby.hub.remote_workspaces as remote_workspaces
from gobby.hub.caller_scope import bind_caller
from gobby.hub.node_client import NodeCommandError
from gobby.hub.remote_workspaces import route_node_workspace_tools
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.services.argument_validation import check_arguments
from gobby.mcp_proxy.tools.clones import create_clones_registry
from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"
SHA = "a" * 40


class _WorkspaceKind(TypedDict):
    table: str
    path: str
    cleanup_days: int


WORKSPACE: dict[str, _WorkspaceKind] = {
    "worktree": {"table": "worktrees", "path": "worktree_path", "cleanup_days": 0},
    "clone": {"table": "clones", "path": "clone_path", "cleanup_days": 7},
}


@pytest.fixture
def hub_machine(temp_db: HubDatabase) -> str:
    machine_id = require_machine_id()
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID)
    return machine_id


@pytest.fixture
def node_machine(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    machine_id = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID)
    temp_db.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) VALUES (%s, %s, %s)",
        (machine_id, sample_project["id"], f"/node-only/{machine_id}/checkout"),
    )
    return machine_id


@pytest.fixture
def node_client(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    client = AsyncMock()
    monkeypatch.setattr(remote_workspaces, "node_command_client", lambda: client)
    return client


def _worktree(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    machine_id: str | None = None,
    detached: bool = False,
) -> str:
    row = LocalWorktreeManager(temp_db).create(
        project_id=sample_project["id"],
        branch_name=None if detached else f"feature/{uuid4().hex}",
        worktree_path=f"/node-only/{uuid4()}/worktree",
    )
    if machine_id is not None:
        temp_db.execute("UPDATE worktrees SET machine_id = %s WHERE id = %s", (machine_id, row.id))
    return row.id


def _clone(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    machine_id: str | None = None,
) -> str:
    row = LocalCloneManager(temp_db).create(
        project_id=sample_project["id"],
        branch_name=f"feature/{uuid4().hex}",
        clone_path=f"/node-only/{uuid4()}/clone",
    )
    if machine_id is not None:
        temp_db.execute("UPDATE clones SET machine_id = %s WHERE id = %s", (machine_id, row.id))
    return row.id


def _session(temp_db: HubDatabase, sample_project: dict[str, Any], machine_id: str) -> str:
    session_id = str(uuid4())
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (session_id, session_id, machine_id, "claude", sample_project["id"]),
    )
    return session_id


def _row(temp_db: HubDatabase, table: str, row_id: str) -> Mapping[str, Any] | None:
    return temp_db.fetchone(f"SELECT * FROM {table} WHERE id = %s", (row_id,))  # nosec B608


def _registries(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    routed: bool = True,
    git_manager: Any = None,
) -> tuple[Any, Any]:
    worktrees = create_worktrees_registry(
        worktree_storage=LocalWorktreeManager(temp_db),
        git_manager=git_manager,
        project_id=sample_project["id"],
    )
    clones = create_clones_registry(
        clone_storage=LocalCloneManager(temp_db),
        git_manager=None,
        project_id=sample_project["id"],
    )
    if routed:
        route_node_workspace_tools(worktrees, kind="worktree", db=temp_db)
        route_node_workspace_tools(clones, kind="clone", db=temp_db)
    return worktrees, clones


def _outcome(outcome: str, **fields: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "outcome": outcome,
        "merge_sha": None,
        "target_head_sha": None,
        "conflicted_files": [],
        "dirty_paths": [],
    }
    reply.update(fields)
    return reply


@pytest.mark.parametrize("kind", ["worktree", "clone"])
async def test_remote_merge_marks_row_merged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    node_client: AsyncMock,
    kind: str,
) -> None:
    node_client.send.return_value = _outcome("merged", merge_sha=SHA, target_head_sha=SHA)
    table, path_column = WORKSPACE[kind]["table"], WORKSPACE[kind]["path"]
    if kind == "worktree":
        row_id = _worktree(temp_db, sample_project, machine_id=node_machine)
    else:
        row_id = _clone(temp_db, sample_project, machine_id=node_machine)
    before = _row(temp_db, table, row_id)
    assert before is not None
    worktrees, clones = _registries(temp_db, sample_project)

    if kind == "worktree":
        result = await worktrees.call(
            "merge_worktree", {"worktree_id": row_id[:12], "target_branch": "main"}
        )
    else:
        result = await clones.call("merge_clone", {"clone_id": row_id, "target_branch": "main"})

    assert result["success"] is True
    assert (result["merge_sha"], result["target_head_sha"]) == (SHA, SHA)
    assert result["machine_id"] == node_machine
    node_client.send.assert_awaited_once()
    machine, command, payload = node_client.send.await_args.args[:3]
    assert (machine, command) == (node_machine, "workspace.merge")
    assert payload == {
        "project_id": sample_project["id"],
        "mode": kind,
        "path": before[path_column],
        "branch_name": before["branch_name"],
        "target_branch": "main",
        "source_root": f"/node-only/{node_machine}/checkout",
    }
    row = _row(temp_db, table, row_id)
    assert row is not None
    assert row["status"] == "merged"
    if kind == "worktree":
        assert row["merged_at"] is not None
    expected_cleanup = datetime.now(UTC) + timedelta(days=WORKSPACE[kind]["cleanup_days"])
    assert abs((row["cleanup_after"] - expected_cleanup).total_seconds()) < 60


@pytest.mark.parametrize(
    ("case", "code", "option"),
    [
        ("no_target", "target_branch_required", None),
        ("push", "remote_merge_option_unsupported", "push"),
        ("prefer_remote", "remote_merge_option_unsupported", "prefer_remote"),
        ("other_source", "remote_merge_option_unsupported", "source_branch"),
        ("no_branch", "branch_required", None),
        ("no_checkout", "checkout_unresolved", None),
        ("conflict", "conflict", None),
        ("target_dirty", "target_dirty", None),
        ("busy", "workspace_busy", None),
    ],
)
async def test_remote_merge_refusals_keep_row(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    node_client: AsyncMock,
    case: str,
    code: str,
    option: str | None,
) -> None:
    row_id = _worktree(
        temp_db,
        sample_project,
        machine_id=node_machine,
        detached=case == "no_branch",
    )
    if case == "no_target":
        temp_db.execute("UPDATE worktrees SET base_branch = NULL WHERE id = %s", (row_id,))
    if case == "no_checkout":
        temp_db.execute("DELETE FROM project_checkouts WHERE machine_id = %s", (node_machine,))
    args: dict[str, Any] = {"worktree_id": row_id}
    if case != "no_target":
        args["target_branch"] = "main"
    if case == "push":
        args["push"] = True
    elif case == "prefer_remote":
        args["prefer_remote"] = True
    elif case == "other_source":
        args["source_branch"] = "feature/other"
    if case == "conflict":
        node_client.send.return_value = _outcome("conflict", conflicted_files=["src/a.py"])
    elif case == "target_dirty":
        node_client.send.return_value = _outcome("target_dirty", dirty_paths=["README.md"])
    elif case == "busy":
        node_client.send.return_value = _outcome("busy")
    before = _row(temp_db, "worktrees", row_id)
    assert before is not None
    worktrees, _ = _registries(temp_db, sample_project)

    result = await worktrees.call("merge_worktree", args)

    assert result["success"] is False
    assert result["error_code"] == code
    if option is not None:
        assert result["option"] == option
    if case == "conflict":
        assert result["has_conflicts"] is True
        assert result["conflicted_files"] == ["src/a.py"]
    if case == "target_dirty":
        assert result["dirty_paths"] == ["README.md"]
    if case in ("conflict", "target_dirty", "busy"):
        node_client.send.assert_awaited_once()
    else:
        node_client.send.assert_not_awaited()
    after = _row(temp_db, "worktrees", row_id)
    assert after is not None
    assert after["status"] == before["status"]
    assert after["merged_at"] is None


@pytest.mark.parametrize("kind", ["worktree", "clone"])
@pytest.mark.parametrize(
    ("reply", "code", "removed"),
    [
        ({"outcome": "removed"}, None, True),
        ({"outcome": "dirty"}, "dirty", False),
        ({"outcome": "unmerged"}, "unmerged", False),
        ({"outcome": "busy"}, "workspace_busy", False),
        (NodeCommandError("node_timeout", "timed out", None), "node_timeout", False),
    ],
)
async def test_remote_delete_follows_node_outcome(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    node_client: AsyncMock,
    kind: str,
    reply: Any,
    code: str | None,
    removed: bool,
) -> None:
    if isinstance(reply, Exception):
        node_client.send.side_effect = reply
    else:
        node_client.send.return_value = reply
    session_id = _session(temp_db, sample_project, node_machine)
    table, path_column = WORKSPACE[kind]["table"], WORKSPACE[kind]["path"]
    if kind == "worktree":
        row_id = _worktree(temp_db, sample_project, machine_id=node_machine)
    else:
        row_id = _clone(temp_db, sample_project, machine_id=node_machine)
    temp_db.execute(
        f"UPDATE {table} SET agent_session_id = %s WHERE id = %s",  # nosec B608
        (session_id, row_id),
    )
    before = _row(temp_db, table, row_id)
    assert before is not None
    worktrees, clones = _registries(temp_db, sample_project)

    if kind == "worktree":
        result = await worktrees.call(
            "delete_worktree",
            {
                "worktree_id": row_id,
                "force": True,
                "force_delete_branch": False,
                "merged_into": "main",
            },
        )
        flags: dict[str, Any] = {"force_delete_branch": False, "merged_into": "main"}
    else:
        result = await clones.call("delete_clone", {"clone_id": row_id, "force": True})
        flags = {"force_delete_branch": False, "merged_into": None}

    node_client.send.assert_awaited_once()
    machine, command, payload = node_client.send.await_args.args[:3]
    assert (machine, command) == (node_machine, "workspace.remove")
    assert payload == {
        "project_id": sample_project["id"],
        "mode": kind,
        "path": before[path_column],
        "branch_name": before["branch_name"],
        "source_root": f"/node-only/{node_machine}/checkout",
        "force": True,
        **flags,
    }
    after = _row(temp_db, table, row_id)
    assert (after is None) is removed
    if removed:
        assert result["success"] is True
        return
    assert after is not None
    assert after["agent_session_id"] == session_id
    assert result["success"] is False
    assert result["error_code"] == code
    if isinstance(reply, NodeCommandError):
        assert result["stage"] == reply.stage


async def test_local_rows_and_schemas_unchanged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    hub_machine: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain_worktrees, plain_clones = _registries(temp_db, sample_project, routed=False)
    routed_worktrees, routed_clones = _registries(temp_db, sample_project)
    for name in ("get_worktree", "merge_worktree", "delete_worktree"):
        assert routed_worktrees.get_schema(name) == plain_worktrees.get_schema(name)
    for name in ("get_clone", "merge_clone", "delete_clone"):
        assert routed_clones.get_schema(name) == plain_clones.get_schema(name)

    local_id = _worktree(temp_db, sample_project)
    ambiguous_a = _worktree(temp_db, sample_project)
    ambiguous_b = _worktree(temp_db, sample_project)
    temp_db.execute(
        "UPDATE worktrees SET id = %s WHERE id = %s",
        ("dead0000-0000-4000-8000-000000000001", ambiguous_a),
    )
    temp_db.execute(
        "UPDATE worktrees SET id = %s WHERE id = %s",
        ("dead0000-0000-4000-8000-000000000002", ambiguous_b),
    )

    lookups: list[str] = []
    real_resolve = LocalWorktreeManager.resolve_reference
    real_get = LocalWorktreeManager.get

    def counting_resolve(self: LocalWorktreeManager, ref: str) -> str:
        lookups.append("resolve_reference")
        return real_resolve(self, ref)

    def counting_get(self: LocalWorktreeManager, worktree_id: str) -> Any:
        lookups.append("get")
        return real_get(self, worktree_id)

    monkeypatch.setattr(LocalWorktreeManager, "resolve_reference", counting_resolve)
    monkeypatch.setattr(LocalWorktreeManager, "get", counting_get)

    async def observe(registry: Any, ref: str) -> tuple[Any, list[str]]:
        lookups.clear()
        result = await registry.call("get_worktree", {"worktree_id": ref})
        return result, list(lookups)

    for ref in (local_id, local_id[:12], "ffffffff", "dead0000"):
        assert await observe(routed_worktrees, ref) == await observe(plain_worktrees, ref)

    local_clone = _clone(temp_db, sample_project)
    for ref in (local_clone, "00000000-0000-4000-8000-00000000dead"):
        routed = await routed_clones.call("get_clone", {"clone_id": ref})
        plain = await plain_clones.call("get_clone", {"clone_id": ref})
        assert routed == plain

    def workspaces(db: HubDatabase | None) -> Any:
        manager = setup_internal_registries(
            config_resolver=lambda: None,
            worktree_storage=LocalWorktreeManager(temp_db),
            clone_storage=LocalCloneManager(temp_db),
            db=db,
        )
        return manager.get_registry("gobby-worktrees")

    assert hasattr(workspaces(temp_db).get_tool_metadata("get_worktree").func, "__wrapped__")
    assert not hasattr(workspaces(None).get_tool_metadata("get_worktree").func, "__wrapped__")


async def test_list_tools_take_machine_id(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
) -> None:
    local_worktree = _worktree(temp_db, sample_project)
    node_worktree = _worktree(temp_db, sample_project, machine_id=node_machine)
    local_clone = _clone(temp_db, sample_project)
    node_clone = _clone(temp_db, sample_project, machine_id=node_machine)
    worktrees, clones = _registries(temp_db, sample_project)
    project_id = sample_project["id"]

    remote_worktrees = await worktrees.call(
        "list_worktrees", {"project_id": project_id, "machine_id": node_machine}
    )
    assert [item["id"] for item in remote_worktrees["worktrees"]] == [node_worktree]
    for args in ({"project_id": project_id}, {"project_id": project_id, "machine_id": None}):
        local_worktrees = await worktrees.call("list_worktrees", args)
        assert [item["id"] for item in local_worktrees["worktrees"]] == [local_worktree]

    remote_clones = await clones.call("list_clones", {"machine_id": node_machine})
    assert [item["id"] for item in remote_clones["clones"]] == [node_clone]
    local_clones = await clones.call("list_clones", {})
    assert [item["id"] for item in local_clones["clones"]] == [local_clone]

    # The proxy strips keys the input schema does not declare before a call reaches the tool.
    for registry, name in ((worktrees, "list_worktrees"), (clones, "list_clones")):
        schema = registry.get_schema(name)
        assert schema is not None
        assert check_arguments({"machine_id": node_machine}, schema["inputSchema"]) == [], name


async def test_offline_and_other_tools_refuse(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    node_client: AsyncMock,
) -> None:
    node_client.send.side_effect = NodeCommandError("node_offline", "offline", "none")
    row_id = _worktree(temp_db, sample_project, machine_id=node_machine)
    worktrees, _ = _registries(temp_db, sample_project, git_manager=MagicMock())

    merged = await worktrees.call(
        "merge_worktree", {"worktree_id": row_id, "target_branch": "main"}
    )

    assert merged["success"] is False
    assert merged["error_code"] == "node_offline"
    assert merged["stage"] == "none"
    row = _row(temp_db, "worktrees", row_id)
    assert row is not None
    assert row["status"] == "active"
    assert row["merged_at"] is None

    released = await worktrees.call("release_worktree", {"worktree_id": row_id})
    assert released["error_code"] == "machine_ownership_mismatch"
    synced = await worktrees.call("sync_worktree", {"worktree_id": row_id})
    assert synced["error_code"] == "machine_ownership_mismatch"


@pytest.mark.usefixtures("hub_machine")
async def test_workspace_tools_need_owned_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    hub_machine: str,
    node_machine: str,
    node_client: AsyncMock,
) -> None:
    local_worktree = _worktree(temp_db, sample_project)
    local_clone = _clone(temp_db, sample_project)
    node_worktree = _worktree(temp_db, sample_project, machine_id=node_machine)
    node_clone = _clone(temp_db, sample_project, machine_id=node_machine)
    local_worktree_row = _row(temp_db, "worktrees", local_worktree)
    local_clone_row = _row(temp_db, "clones", local_clone)
    assert local_worktree_row is not None
    assert local_clone_row is not None
    worktrees, clones = _registries(temp_db, sample_project)
    project_id = sample_project["id"]

    refused: list[tuple[Any, str, dict[str, Any]]] = [
        (worktrees, "get_worktree", {"worktree_id": local_worktree}),
        (worktrees, "get_worktree", {"worktree_id": node_worktree}),
        (worktrees, "get_worktree", {"worktree_id": "00000000-0000-4000-8000-00000000dead"}),
        (worktrees, "merge_worktree", {"worktree_id": node_worktree, "target_branch": "main"}),
        (worktrees, "delete_worktree", {"worktree_id": local_worktree}),
        (worktrees, "delete_worktree", {"worktree_id": node_worktree}),
        (worktrees, "delete_worktree", {"worktree_path": local_worktree_row["worktree_path"]}),
        (worktrees, "list_worktrees", {"project_id": project_id}),
        (worktrees, "list_worktrees", {"project_id": project_id, "machine_id": None}),
        (worktrees, "list_worktrees", {"project_id": project_id, "machine_id": hub_machine}),
        (worktrees, "list_worktrees", {"project_id": project_id, "machine_id": node_machine}),
        (clones, "get_clone", {"clone_id": local_clone}),
        (clones, "get_clone", {"clone_id": node_clone}),
        (clones, "merge_clone", {"clone_id": node_clone, "target_branch": "main"}),
        (clones, "delete_clone", {"clone_id": local_clone}),
        (clones, "delete_clone", {"clone_id": node_clone}),
        (clones, "delete_clone", {"clone_path": local_clone_row["clone_path"]}),
        (clones, "list_clones", {}),
        (clones, "list_clones", {"machine_id": None}),
        (clones, "list_clones", {"machine_id": hub_machine}),
        (clones, "list_clones", {"machine_id": node_machine}),
    ]
    other = bind_caller(OTHER_USER_ID, temp_db)
    try:
        for registry, name, args in refused:
            result = await registry.call(name, args)
            assert result["error_code"] == "machine_not_owned", name
    finally:
        other.var.reset(other)
    node_client.send.assert_not_awaited()
    row = _row(temp_db, "worktrees", node_worktree)
    assert row is not None
    assert row["status"] == "active"
    assert _row(temp_db, "worktrees", local_worktree) == local_worktree_row
    assert _row(temp_db, "clones", local_clone) == local_clone_row

    owner = bind_caller(TEST_USER_ID, temp_db)
    try:
        owned = await worktrees.call("get_worktree", {"worktree_id": node_worktree})
        assert owned["success"] is True
        owned_local = await worktrees.call("get_worktree", {"worktree_id": local_worktree})
        assert owned_local["success"] is True
        owned_list = await worktrees.call("list_worktrees", {"project_id": project_id})
        assert owned_list["success"] is True
        assert local_worktree in [item["id"] for item in owned_list["worktrees"]]
        owned_clones = await clones.call("list_clones", {"machine_id": None})
        assert owned_clones["success"] is True
        assert local_clone in [item["id"] for item in owned_clones["clones"]]
    finally:
        owner.var.reset(owner)

    unbound = await worktrees.call("get_worktree", {"worktree_id": node_worktree})
    assert unbound["success"] is True
    unbound_list = await worktrees.call("list_worktrees", {"project_id": project_id})
    assert unbound_list["success"] is True


@pytest.mark.usefixtures("hub_machine")
@pytest.mark.parametrize("tool", ["get_worktree", "abandon_worktree", "sync_worktree"])
async def test_ambiguous_prefix_hides_foreign_ids(
    tool: str,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
) -> None:
    # 17 ids over 16 hex first characters guarantee two rows share a prefix.
    node_ids = [_worktree(temp_db, sample_project, machine_id=node_machine) for _ in range(17)]
    by_first_char: dict[str, list[str]] = {}
    for row_id in node_ids:
        by_first_char.setdefault(row_id[0], []).append(row_id)
    prefix = next(char for char, ids in by_first_char.items() if len(ids) > 1)
    worktrees, _ = _registries(temp_db, sample_project)

    other = bind_caller(OTHER_USER_ID, temp_db)
    try:
        refused = await worktrees.call(tool, {"worktree_id": prefix})
    finally:
        other.var.reset(other)
    assert refused.get("error_code") == "machine_not_owned", (tool, refused)

    owner = bind_caller(TEST_USER_ID, temp_db)
    try:
        ambiguous = await worktrees.call(tool, {"worktree_id": prefix})
    finally:
        owner.var.reset(owner)
    assert ambiguous.get("error_code") == "ambiguous_reference", (tool, ambiguous)
    for result in (refused, ambiguous):
        assert not any(row_id in str(result) for row_id in node_ids)


@pytest.mark.usefixtures("hub_machine")
async def test_list_refusal_precedes_project_resolution(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
) -> None:
    worktrees, _ = _registries(temp_db, sample_project)
    other = bind_caller(OTHER_USER_ID, temp_db)
    try:
        result = await worktrees.call(
            "list_worktrees",
            {"project_path": "/node-only/no-such-project", "machine_id": node_machine},
        )
    finally:
        other.var.reset(other)
    assert result["error_code"] == "machine_not_owned"


@pytest.mark.usefixtures("hub_machine")
async def test_sibling_workspace_tools_need_owned_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    hub_machine: str,
    node_client: AsyncMock,
) -> None:
    session_id = _session(temp_db, sample_project, hub_machine)
    local_worktree = _worktree(temp_db, sample_project)
    local_clone = _clone(temp_db, sample_project)
    temp_db.execute(
        "UPDATE clones SET agent_session_id = %s WHERE id = %s", (session_id, local_clone)
    )
    worktree_before = _row(temp_db, "worktrees", local_worktree)
    clone_before = _row(temp_db, "clones", local_clone)
    assert worktree_before is not None
    assert clone_before is not None
    worktrees, clones = _registries(temp_db, sample_project)

    refused: list[tuple[Any, str, dict[str, Any]]] = [
        (worktrees, "get_worktree_stats", {}),
        (worktrees, "get_worktree_by_task", {"task_id": "#1"}),
        (worktrees, "create_worktree", {"branch_name": "feature/refused"}),
        (worktrees, "abandon_worktree", {"worktree_id": local_worktree}),
        (worktrees, "reactivate_worktree", {"worktree_id": local_worktree}),
        (worktrees, "mark_worktree_merged", {"worktree_id": local_worktree}),
        (worktrees, "claim_worktree", {"worktree_id": local_worktree}),
        (worktrees, "release_worktree", {"worktree_id": local_worktree}),
        (worktrees, "link_task_to_worktree", {"worktree_id": local_worktree, "task_id": "#1"}),
        (worktrees, "sync_worktree", {"worktree_id": local_worktree}),
        (worktrees, "push_branch", {"worktree_id": local_worktree}),
        (worktrees, "detect_stale_worktrees", {}),
        (worktrees, "cleanup_stale_worktrees", {}),
        (clones, "get_clone_stats", {}),
        (clones, "get_clone_by_task", {"task_id": "#1"}),
        (clones, "create_clone", {"branch_name": "feature/refused"}),
        (clones, "claim_clone", {"clone_id": local_clone}),
        (clones, "release_clone", {"clone_id": local_clone}),
        (clones, "link_task_to_clone", {"clone_id": local_clone, "task_id": "#1"}),
        (clones, "sync_clone", {"clone_id": local_clone}),
    ]
    other = bind_caller(OTHER_USER_ID, temp_db)
    try:
        for registry, name, args in refused:
            result = await registry.call(name, args)
            assert result.get("error_code") == "machine_not_owned", (name, result)
    finally:
        other.var.reset(other)
    node_client.send.assert_not_awaited()
    assert _row(temp_db, "worktrees", local_worktree) == worktree_before
    assert _row(temp_db, "clones", local_clone) == clone_before


@pytest.mark.usefixtures("hub_machine")
async def test_sibling_workspace_tools_owner_and_unbound_proceed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    hub_machine: str,
    node_machine: str,
) -> None:
    session_id = _session(temp_db, sample_project, hub_machine)
    owned_worktree = _worktree(temp_db, sample_project)
    unbound_worktree = _worktree(temp_db, sample_project)
    owned_clone = _clone(temp_db, sample_project)
    temp_db.execute(
        "UPDATE clones SET agent_session_id = %s WHERE id = %s", (session_id, owned_clone)
    )
    node_worktree = _worktree(temp_db, sample_project, machine_id=node_machine)
    worktrees, clones = _registries(temp_db, sample_project)

    owner = bind_caller(TEST_USER_ID, temp_db)
    try:
        abandoned = await worktrees.call("abandon_worktree", {"worktree_id": owned_worktree})
        assert abandoned["success"] is True
        released = await clones.call("release_clone", {"clone_id": owned_clone})
        assert released["success"] is True
        remote = await worktrees.call(
            "list_worktrees", {"project_id": sample_project["id"], "machine_id": node_machine}
        )
        assert [item["id"] for item in remote["worktrees"]] == [node_worktree]
    finally:
        owner.var.reset(owner)
    owned_row = _row(temp_db, "worktrees", owned_worktree)
    assert owned_row is not None
    assert owned_row["status"] == "abandoned"
    owned_clone_row = _row(temp_db, "clones", owned_clone)
    assert owned_clone_row is not None
    assert owned_clone_row["agent_session_id"] is None

    unbound = await worktrees.call("abandon_worktree", {"worktree_id": unbound_worktree})
    assert unbound["success"] is True


def test_list_remote_applies_session_filter_and_limit_in_query(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
) -> None:
    session_id = _session(temp_db, sample_project, node_machine)
    other_session_id = _session(temp_db, sample_project, node_machine)
    for owner_session in (session_id, session_id, session_id, other_session_id):
        row_id = _worktree(temp_db, sample_project, machine_id=node_machine)
        temp_db.execute(
            "UPDATE worktrees SET agent_session_id = %s WHERE id = %s",
            (owner_session, row_id),
        )

    rows = remote_workspaces.list_remote(
        temp_db,
        "worktree",
        node_machine,
        project_id=sample_project["id"],
        agent_session_id=session_id,
        limit=2,
    )

    assert len(rows) == 2
    assert {row.agent_session_id for row in rows} == {session_id}


@pytest.mark.parametrize("kind", ["worktree", "clone"])
def test_list_remote_matches_local_defaults(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    kind: remote_workspaces.WorkspaceKind,
) -> None:
    create = _worktree if kind == "worktree" else _clone
    table = WORKSPACE[kind]["table"]
    created_at = datetime(2020, 1, 1, tzinfo=UTC)
    for index in range(51):
        row_id = create(temp_db, sample_project)
        temp_db.execute(
            f"UPDATE {table} SET created_at = %s WHERE id = %s",
            (created_at + timedelta(seconds=index), row_id),
        )
    if kind == "worktree":
        expected_ids = [
            row.id
            for row in LocalWorktreeManager(temp_db).list_worktrees(project_id=sample_project["id"])
        ]
    else:
        cleanup_id = _clone(temp_db, sample_project)
        temp_db.execute(
            "UPDATE clones SET status = 'cleanup', created_at = %s WHERE id = %s",
            (created_at + timedelta(seconds=51), cleanup_id),
        )
        expected_ids = [
            row.id
            for row in LocalCloneManager(temp_db).list_clones(project_id=sample_project["id"])
        ]
    temp_db.execute(
        f"UPDATE {table} SET machine_id = %s WHERE project_id = %s",
        (node_machine, sample_project["id"]),
    )

    rows = remote_workspaces.list_remote(
        temp_db, kind, node_machine, project_id=sample_project["id"]
    )

    assert len(rows) == 50
    assert [row.id for row in rows] == expected_ids


@pytest.mark.parametrize("status", [None, "cleanup", "active"])
def test_list_remote_excludes_cleanup_clones(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    node_machine: str,
    status: str | None,
) -> None:
    active = _clone(temp_db, sample_project, machine_id=node_machine)
    cleanup = _clone(temp_db, sample_project, machine_id=node_machine)
    temp_db.execute("UPDATE clones SET status = 'cleanup' WHERE id = %s", (cleanup,))

    rows = remote_workspaces.list_remote(temp_db, "clone", node_machine, status=status)

    assert [row.id for row in rows] == ([] if status == "cleanup" else [active])
