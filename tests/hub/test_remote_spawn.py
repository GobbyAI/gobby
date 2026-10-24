"""Remote spawn: a run bound for another machine launches through that machine's node.

Every row a remote spawn writes names the target machine before the node is sent a command,
the hub writes nothing to its own disk or credential store for that run, and every failure
ends in one owned state: failed and settled, or in doubt with its rows kept.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import gobby.agents.spawn as spawn_module
import gobby.mcp_proxy.tools.spawn_agent._factory as factory
import gobby.mcp_proxy.tools.spawn_agent._spawn_guards as spawn_guards
from gobby.agents import srt_runtime
from gobby.agents.session import ChildSessionManager
from gobby.agents.spawners.prompt_manager import MAX_ENV_PROMPT_LENGTH
from gobby.config.app import DaemonConfig
from gobby.config.runtime_models import ConfigSnapshot
from gobby.hub import machine_routing, remote_spawn
from gobby.hub.caller_scope import bind_caller, reset_caller
from gobby.hub.machine_routing import RoutedMachine
from gobby.hub.node_client import NodeCommandError
from gobby.hub.remote_spawn import RemoteSpawnRequest, spawn_remote_agent
from gobby.mcp_proxy.tools.spawn_agent._factory import create_spawn_agent_registry
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import TaskSpawnLease
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager, MachineNotRegisteredError
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import (
    TEST_FRONT_DOOR_IDENTITY,
    TEST_FRONT_DOOR_SECRET,
    StubConfigRuntime,
    authenticate_as,
    create_http_server,
)

pytestmark = pytest.mark.unit

OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture
def local_machine(temp_db: HubDatabase) -> str:
    machine_id = require_machine_id()
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID)
    return machine_id


@pytest.fixture
def remote_machine(temp_db: HubDatabase, sample_project: dict[str, Any]) -> str:
    machine_id = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID)
    temp_db.execute(
        "INSERT INTO project_checkouts(machine_id, project_id, root_path) VALUES (%s, %s, %s)",
        (machine_id, sample_project["id"], f"/node-only/{machine_id}/checkout"),
    )
    return machine_id


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch, remote_machine: str) -> AsyncMock:
    client = AsyncMock()
    client.connected_machine_ids.return_value = {remote_machine}
    client.send.return_value = {"accepted": True}
    monkeypatch.setattr(machine_routing, "node_command_client", lambda: client)
    monkeypatch.setattr(remote_spawn, "node_command_client", lambda: client, raising=False)
    return client


@pytest.fixture
def sessions(temp_db: HubDatabase) -> ChildSessionManager:
    return ChildSessionManager(session_storage=SessionManager(temp_db), max_agent_depth=5)


@pytest.fixture
def runner(temp_db: HubDatabase) -> MagicMock:
    stub = MagicMock()
    stub.can_spawn.return_value = (True, "", 0)
    stub.run_storage = LocalAgentRunManager(temp_db)
    return stub


@pytest.fixture
def remote_parent(temp_db: HubDatabase, sample_project: dict[str, Any], remote_machine: str) -> str:
    return _insert_session(temp_db, sample_project["id"], remote_machine)


@pytest.fixture
def local_parent(temp_db: HubDatabase, sample_project: dict[str, Any], local_machine: str) -> str:
    return _insert_session(temp_db, sample_project["id"], local_machine)


@pytest.fixture
def spawn_registry(temp_db: HubDatabase, runner: MagicMock) -> Any:
    return create_spawn_agent_registry(
        runner,
        task_manager=LocalTaskManager(temp_db),
        session_manager=SessionManager(temp_db),
        db=temp_db,
        config_resolver=lambda: DaemonConfig(),
    )


def _insert_session(
    db: HubDatabase, project_id: str, machine_id: str, status: str = "active"
) -> str:
    session_id = str(uuid4())
    db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id, status)
        VALUES (%s, %s, %s, 'claude', %s, %s)
        """,
        (session_id, f"remote-spawn-{session_id}", machine_id, project_id, status),
    )
    return session_id


def _run_count(db: HubDatabase) -> int:
    row = db.fetchone("SELECT COUNT(*) AS n FROM agent_runs", ())
    return int(row["n"]) if row else 0


def _routed(machine_id: str) -> RoutedMachine:
    return RoutedMachine(machine_id, local=False, checkout_root=f"/node-only/{machine_id}/checkout")


def _request(parent_session_id: str, project_id: str, **overrides: Any) -> RemoteSpawnRequest:
    fields: dict[str, Any] = {"prompt": "Review the branch", "provider": "claude", **overrides}
    return RemoteSpawnRequest(parent_session_id=parent_session_id, project_id=project_id, **fields)


def _owners(db: HubDatabase, run_id: str) -> dict[str, str | None]:
    run = db.fetchone(
        "SELECT machine_id, child_session_id FROM agent_runs WHERE id = %s", (run_id,)
    )
    assert run is not None
    session = db.fetchone(
        "SELECT machine_id FROM sessions WHERE id = %s", (run["child_session_id"],)
    )
    terminal = db.fetchone("SELECT machine_id FROM terminals WHERE agent_run_id = %s", (run_id,))
    assert session is not None and terminal is not None
    return {
        "run": str(run["machine_id"]),
        "session": str(session["machine_id"]),
        "terminal": str(terminal["machine_id"]),
    }


def _row_counts(db: HubDatabase) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("sessions", "agent_runs", "terminals", "worktrees", "clones"):
        row = db.fetchone(f"SELECT COUNT(*) AS n FROM {table}", ())
        assert row is not None
        counts[table] = int(row["n"])
    return counts


def _state(db: HubDatabase, table: str, run_id: str) -> str | None:
    """Status of the run row, or the state of the terminal bound to that run."""
    if table == "terminals":
        row = db.fetchone("SELECT state FROM terminals WHERE agent_run_id = %s", (run_id,))
        return None if row is None else str(row["state"])
    row = db.fetchone("SELECT status FROM agent_runs WHERE id = %s", (run_id,))
    return None if row is None else str(row["status"])


def _fill_machine(db: HubDatabase, project_id: str, machine_id: str, count: int) -> None:
    manager = TerminalManager(db)
    for _ in range(count):
        terminal_id = str(uuid4())
        manager.create_pending(
            terminal_id=terminal_id,
            project_id=project_id,
            backend="native",
            ownership="gobby",
            spawn_key=f"gobby-{terminal_id}",
            machine_id=machine_id,
        )


def _lease() -> MagicMock:
    lease = MagicMock()
    lease.acquire.return_value = None
    lease.attach.return_value = None
    return lease


@contextmanager
def _owner_http_client(db: HubDatabase, user_id: str, runner: MagicMock) -> Iterator[TestClient]:
    """Front-door HTTP client whose real middleware binds ``user_id`` as the caller."""
    config = DaemonConfig()
    server = create_http_server(
        config=config,
        database=db,
        session_manager=SessionManager(db),
        task_manager=LocalTaskManager(db),
        agent_runner=runner,
    )
    server.services.config_runtime = StubConfigRuntime(
        ConfigSnapshot(
            revision=1,
            desired=config,
            active=config,
            row_revisions={},
            pending_restart_keys=frozenset(),
            failed_live_keys={},
            desired_values={},
            active_values={},
        )
    )
    server.auth_service.bind_runtime(
        grant_service=None,
        local_machine_id=TEST_FRONT_DOOR_IDENTITY["X-Gobby-Machine-Id"],
        clock=None,
        front_door_secret=TEST_FRONT_DOOR_SECRET,
    )
    server = authenticate_as(server, user_id)
    with patch("gobby.servers.app_factory.HookManager") as hook_manager:
        hook_manager.return_value._stop_registry = MagicMock()
        hook_manager.return_value.shutdown = MagicMock()
        hook_manager.return_value.shutdown_async = AsyncMock()
        with TestClient(server.app, headers=TEST_FRONT_DOOR_IDENTITY) as client:
            yield client


async def test_spawn_routes_to_requesting_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
) -> None:
    seen: dict[str, str | None] = {}

    async def _send(machine_id: str, command: str, payload: dict[str, Any], *args: Any) -> Any:
        seen.update(_owners(temp_db, payload["run_id"]))
        return {"accepted": True}

    node.send.side_effect = _send

    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"]),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    node.send.assert_awaited_once()
    (machine_id, command, payload), _ = node.send.await_args
    assert (machine_id, command) == (remote_machine, "agent.spawn")
    assert seen == {"run": remote_machine, "session": remote_machine, "terminal": remote_machine}
    assert result["success"] is True
    assert result["status"] == "pending"
    assert result["run_id"] == payload["run_id"]


async def test_remote_spawn_touches_no_hub_disk(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_builder = MagicMock(side_effect=AssertionError("hub built env for a remote run"))
    monkeypatch.setattr(spawn_module, "get_terminal_env_vars", env_builder)
    prompt = "p" * (MAX_ENV_PROMPT_LENGTH + 1)

    await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"], prompt=prompt),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    env_builder.assert_not_called()
    node.send.assert_awaited_once()
    (_, _, payload), _ = node.send.await_args
    assert payload["prompt"] == prompt


async def test_explicit_machine_is_validated(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    no_checkout = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(no_checkout, TEST_USER_ID)
    before = _row_counts(temp_db)

    refusals = {}
    for machine, code in (
        (no_checkout, "checkout_unresolved"),
        (str(uuid4()), "machine_not_found"),
    ):
        refusals[code] = await spawn_registry.call(
            "spawn_agent",
            {"prompt": "x", "parent_session_id": local_parent, "machine": machine},
        )

    assert refusals["checkout_unresolved"]["error_code"] == "checkout_unresolved"
    assert refusals["machine_not_found"]["error_code"] == "machine_not_found"
    assert all(result["success"] is False for result in refusals.values())
    node.send.assert_not_awaited()
    impl.assert_not_awaited()
    assert _row_counts(temp_db) == before


async def test_invalid_model_refusal_releases_task_lease(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        title="Remote spawn",
        task_type="task",
        project_id=sample_project["id"],
        validation_criteria="Spawn is observable.",
    )
    lease = TaskSpawnLease(db=temp_db, task_id=task.id)

    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"], task_id=task.id, model="local"),
        lease=lease,
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_model"
    node.send.assert_not_awaited()
    # A refusal before any row or send holds no task lease: a retry for the task can take it.
    retry = TaskSpawnLease(db=temp_db, task_id=task.id)
    assert retry.acquire() is None
    retry.release_unattached()


async def test_remote_resume_refuses_another_project(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    other = install_isolated_checkout_project(
        temp_db, tmp_path / "other-checkout", name="other-project", machine_id=require_machine_id()
    )
    foreign = _insert_session(temp_db, other.project.id, remote_machine)
    runs_before = _run_count(temp_db)

    refused = await spawn_registry.call(
        "spawn_agent",
        {"prompt": "x", "parent_session_id": local_parent, "resume_session_id": foreign},
    )

    assert refused["success"] is False
    assert refused["error_code"] == "resume_refused"
    assert "another project" in refused["error"]
    node.send.assert_not_awaited()
    assert _run_count(temp_db) == runs_before
    impl.assert_not_awaited()


async def test_remote_resume_refuses_live_target(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    live = _insert_session(temp_db, sample_project["id"], remote_machine)
    temp_db.execute("UPDATE sessions SET status = 'active' WHERE id = %s", (live,))
    runs_before = _run_count(temp_db)

    refused = await spawn_registry.call(
        "spawn_agent",
        {"prompt": "x", "parent_session_id": local_parent, "resume_session_id": live},
    )

    assert refused["success"] is False
    assert refused["error_code"] == "resume_refused"
    assert "still live" in refused["error"]
    node.send.assert_not_awaited()
    assert _run_count(temp_db) == runs_before
    impl.assert_not_awaited()


async def test_resume_by_reference_routes_to_owner(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    node_session = _insert_session(temp_db, sample_project["id"], remote_machine, status="expired")
    temp_db.execute(
        """
        INSERT INTO worktrees (id, project_id, machine_id, branch_name, worktree_path,
            base_branch, agent_session_id, status, created_at, updated_at)
        VALUES (%s, %s, %s, 'agent/resumed', '/remote/worktrees/resumed', '0.5.0', %s,
            'active', now(), now())
        """,
        (str(uuid4()), sample_project["id"], remote_machine, node_session),
    )

    resumed = await spawn_registry.call(
        "spawn_agent",
        {"prompt": "x", "parent_session_id": local_parent, "resume_session_id": node_session[:8]},
    )

    assert resumed["success"] is True
    node.send.assert_awaited_once()
    (machine_id, command, payload), _ = node.send.await_args
    assert (machine_id, command) == (remote_machine, "agent.spawn")
    assert payload["resume_session_id"] == f"remote-spawn-{node_session}"
    impl.assert_not_awaited()


async def test_closed_task_spawn_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    task = LocalTaskManager(temp_db).create_task(
        title="Closed remote work",
        task_type="task",
        project_id=sample_project["id"],
        validation_criteria="Done.",
    )
    LocalTaskManager(temp_db).close_task(task.id)
    runs_before = _run_count(temp_db)
    sessions_before = _row_counts(temp_db)["sessions"]

    refused = await spawn_registry.call(
        "spawn_agent",
        {
            "prompt": "x",
            "parent_session_id": local_parent,
            "task_id": task.id,
            "machine": remote_machine,
        },
    )

    assert refused["success"] is False
    node.send.assert_not_awaited()
    assert _run_count(temp_db) == runs_before
    assert _row_counts(temp_db)["sessions"] == sessions_before
    impl.assert_not_awaited()


async def test_remote_child_binds_run_and_variables(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_parent: str,
    remote_machine: str,
    node: AsyncMock,
    sessions: ChildSessionManager,
    runner: MagicMock,
) -> None:
    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(
            remote_parent, sample_project["id"], initial_variables={"_agent_type": "developer"}
        ),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert result["success"] is True
    session = temp_db.fetchone(
        "SELECT agent_run_id FROM sessions WHERE id = %s", (result["child_session_id"],)
    )
    assert session is not None and session["agent_run_id"] == result["run_id"]
    variables = SessionVariableManager(temp_db).get_variable_subset(
        result["child_session_id"], ("_agent_type",)
    )
    assert variables == {"_agent_type": "developer"}


async def test_reserved_run_id_names_the_run(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_parent: str,
    remote_machine: str,
    node: AsyncMock,
    sessions: ChildSessionManager,
    runner: MagicMock,
) -> None:
    reserved = str(uuid4())

    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"], reserved_run_id=reserved),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert result["success"] is True
    assert result["run_id"] == reserved
    (_, _, payload), _ = node.send.await_args
    assert payload["run_id"] == reserved


async def test_prepare_failure_releases_task_lease(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_parent: str,
    remote_machine: str,
    node: AsyncMock,
    sessions: ChildSessionManager,
    runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = LocalTaskManager(temp_db).create_task(
        title="Prepare fails",
        task_type="task",
        project_id=sample_project["id"],
        validation_criteria="Done.",
    )
    prepare = MagicMock(side_effect=RuntimeError("prepare broke"))
    monkeypatch.setattr(remote_spawn, "prepare_terminal_spawn", prepare)
    lease = TaskSpawnLease(db=temp_db, task_id=task.id)

    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"], task_id=task.id),
        lease=lease,
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert result["success"] is False
    assert result["error_code"] == "prepare_failed"
    prepare.assert_called_once()
    node.send.assert_not_awaited()
    retry = TaskSpawnLease(db=temp_db, task_id=task.id)
    assert retry.acquire() is None
    retry.release_unattached()


async def test_failed_terminal_row_rolls_back_run(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_parent: str,
    remote_machine: str,
    node: AsyncMock,
    sessions: ChildSessionManager,
    runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollback = AsyncMock()
    monkeypatch.setattr(remote_spawn, "_roll_back_run", rollback)
    monkeypatch.setattr(
        TerminalManager, "create_pending", MagicMock(side_effect=RuntimeError("row failed"))
    )

    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"]),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert result["success"] is False
    rollback.assert_awaited_once()
    assert rollback.await_args is not None
    assert rollback.await_args.args[1] == result["run_id"]
    node.send.assert_not_awaited()


async def test_project_cap_counts_remote_runs(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn_guards, "configured_max_active_agents", lambda: 1)
    args = {"prompt": "x", "parent_session_id": local_parent, "machine": remote_machine}

    first = await spawn_registry.call("spawn_agent", args)
    second = await spawn_registry.call("spawn_agent", args)

    assert first["success"] is True
    assert second["success"] is False
    assert second["error_code"] == "project_cap_reached"
    assert node.send.await_count == 1


async def test_http_origin_is_the_front_door_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    node: AsyncMock,
    runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    task = LocalTaskManager(temp_db).create_task(
        title="Front door origin",
        task_type="task",
        project_id=sample_project["id"],
        validation_criteria="Spawn is observable.",
    )
    temp_db.execute(
        "INSERT INTO users (id, email, name, password_hash) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (id) DO NOTHING",
        (OTHER_USER_ID, "other@example.test", "Other", "unused"),
    )
    foreign = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(foreign, OTHER_USER_ID)
    body = {"task_id": task.id, "prompt": "x", "machine": remote_machine, "provider": "claude"}

    with _owner_http_client(temp_db, TEST_USER_ID, runner) as client:
        refused = client.post(
            "/api/agents/spawn", json=body, headers={"X-Gobby-Machine-Id": foreign}
        )

    assert refused.json()["error_code"] == "machine_not_owned"
    node.send.assert_not_awaited()


async def test_definite_failure_settles_in_database(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_kill = MagicMock()
    monkeypatch.setattr(spawn_module, "_terminate_spawn_process", local_kill, raising=False)
    task = LocalTaskManager(temp_db).create_task(
        title="Remote spawn",
        task_type="task",
        project_id=sample_project["id"],
        validation_criteria="Spawn is observable.",
    )
    lease = TaskSpawnLease(db=temp_db, task_id=task.id)
    run_ids: list[str] = []

    async def _send(machine_id: str, command: str, payload: dict[str, Any], *args: Any) -> Any:
        run_ids.append(payload["run_id"])
        raise NodeCommandError("node_offline", "Target has no live channel", "none")

    node.send.side_effect = _send

    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"], task_id=task.id),
        lease=lease,
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert result["success"] is False
    assert result["error_code"] == "node_offline"
    assert result["stage"] == "none"
    (run_id,) = run_ids
    assert _state(temp_db, "agent_runs", run_id) == "cancelled"
    assert _state(temp_db, "terminals", run_id) == "exited"
    local_kill.assert_not_called()
    node.send.assert_awaited_once()
    # The definite failure ends the task lease: a retry for the same task can take it.
    retry = TaskSpawnLease(db=temp_db, task_id=task.id)
    assert retry.acquire() is None
    retry.release_unattached()


async def test_lease_attach_failure_refuses_before_send(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskSpawnLease, "attach", lambda self, run_id: "mutex row missing")
    task = LocalTaskManager(temp_db).create_task(
        title="Remote spawn",
        task_type="task",
        project_id=sample_project["id"],
        validation_criteria="Spawn is observable.",
    )

    result = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"], task_id=task.id),
        lease=TaskSpawnLease(db=temp_db, task_id=task.id),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert result["success"] is False
    assert result["error_code"] == "spawn_mutex_failed"
    assert result["stage"] == "none"
    node.send.assert_not_awaited()
    run_id = result["run_id"]
    assert _state(temp_db, "agent_runs", run_id) == "cancelled"
    assert _state(temp_db, "terminals", run_id) == "exited"
    retry = TaskSpawnLease(db=temp_db, task_id=task.id)
    assert retry.acquire() is None
    retry.release_unattached()


async def test_in_doubt_spawn_keeps_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
) -> None:
    for code in ("node_timeout", "node_reply_lost"):
        node.send.side_effect = NodeCommandError(code, "no reply", None)
        lease = _lease()

        result = await spawn_remote_agent(
            temp_db,
            routed=_routed(remote_machine),
            request=_request(remote_parent, sample_project["id"]),
            lease=lease,
            daemon_config=DaemonConfig(),
            runner=runner,
            session_manager=sessions,
        )

        assert result["success"] is False
        assert result["error_code"] == "spawn_in_doubt"
        (_, _, payload), _ = node.send.await_args
        run_id = payload["run_id"]
        assert result["run_id"] == run_id
        assert _state(temp_db, "agent_runs", run_id) == "pending"
        assert _state(temp_db, "terminals", run_id) == "pending"
        lease.attach.assert_called_once_with(run_id)
        lease.release_unattached.assert_not_called()


async def test_origin_is_the_calling_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)

    # A caller on the node, whose parent is on the hub, spawns on the node.
    monkeypatch.setattr(factory, "enforce_spawn_caller", AsyncMock(return_value=remote_parent))
    node_caller = await spawn_registry.call(
        "spawn_agent", {"prompt": "x", "parent_session_id": local_parent}
    )
    assert node_caller["success"] is True
    assert node_caller["status"] == "pending"
    impl.assert_not_awaited()

    # A caller on the hub, whose parent is on the node, spawns locally in the hub's checkout.
    monkeypatch.setattr(factory, "enforce_spawn_caller", AsyncMock(return_value=local_parent))
    hub_caller = await spawn_registry.call(
        "spawn_agent", {"prompt": "x", "parent_session_id": remote_parent}
    )
    assert hub_caller["success"] is True
    impl.assert_awaited_once()
    node_sends = [call.args[0] for call in node.send.await_args_list]
    assert node_sends == [remote_machine]


async def test_resume_routes_and_placement_refused(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    node_session = _insert_session(temp_db, sample_project["id"], remote_machine, status="expired")
    temp_db.execute(
        """
        INSERT INTO worktrees (id, project_id, machine_id, branch_name, worktree_path,
            base_branch, agent_session_id, status, created_at, updated_at)
        VALUES (%s, %s, %s, 'agent/resumed', '/remote/worktrees/resumed', '0.5.0', %s,
            'active', now(), now())
        """,
        (str(uuid4()), sample_project["id"], remote_machine, node_session),
    )

    placed = await spawn_registry.call(
        "spawn_agent",
        {
            "prompt": "x",
            "parent_session_id": local_parent,
            "machine": remote_machine,
            "placement": {"project": sample_project["id"]},
        },
    )
    assert placed["success"] is False
    assert placed["error_code"] == "remote_placement_unsupported"
    node.send.assert_not_awaited()

    resumed = await spawn_registry.call(
        "spawn_agent",
        {"prompt": "x", "parent_session_id": local_parent, "resume_session_id": node_session},
    )
    assert resumed["success"] is True
    node.send.assert_awaited_once()
    (machine_id, command, payload), _ = node.send.await_args
    assert (machine_id, command) == (remote_machine, "agent.spawn")
    assert payload["resume_session_id"] == f"remote-spawn-{node_session}"
    assert payload["workspace"]["mode"] == "existing"
    assert payload["workspace"]["path"] == "/remote/worktrees/resumed"
    assert all(owner == remote_machine for owner in _owners(temp_db, payload["run_id"]).values())
    impl.assert_not_awaited()


async def test_local_target_unchanged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True, "run_id": "local-run"})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)

    result = await spawn_registry.call(
        "spawn_agent", {"prompt": "x", "parent_session_id": local_parent}
    )

    assert result == {"success": True, "run_id": "local-run"}
    impl.assert_awaited_once()
    node.send.assert_not_awaited()


async def test_remote_target_needs_no_hub_checkout(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    local_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    temp_db.execute("DELETE FROM project_checkouts WHERE machine_id = %s", (local_machine,))

    mcp_result = await spawn_registry.call(
        "spawn_agent", {"prompt": "x", "parent_session_id": local_parent, "machine": remote_machine}
    )

    assert mcp_result["success"] is True
    assert mcp_result["status"] == "pending"
    node.send.assert_awaited_once()
    impl.assert_not_awaited()


async def test_foreign_session_needs_routed_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_machine: str,
    remote_machine: str,
) -> None:
    manager = SessionManager(temp_db)
    unenrolled = str(uuid4())

    with pytest.raises(MachineOwnershipMismatchError):
        manager.register(
            external_id="foreign-plain",
            machine_id=remote_machine,
            source="claude",
            project_id=sample_project["id"],
        )
    with pytest.raises(MachineOwnershipMismatchError):
        manager.register_session(
            external_id="foreign-session",
            machine_id=remote_machine,
            source="claude",
            project_id=sample_project["id"],
        )
    with pytest.raises(MachineOwnershipMismatchError):
        manager.register(
            external_id="foreign-misrouted",
            machine_id=remote_machine,
            source="claude",
            project_id=sample_project["id"],
            routed_machine_id=local_machine,
        )
    with pytest.raises(MachineNotRegisteredError):
        manager.register(
            external_id="foreign-unenrolled",
            machine_id=unenrolled,
            source="claude",
            project_id=sample_project["id"],
            routed_machine_id=unenrolled,
        )

    routed = manager.register(
        external_id="routed-remote",
        machine_id=remote_machine,
        source="claude",
        project_id=sample_project["id"],
        routed_machine_id=remote_machine,
    )
    assert str(routed.machine_id) == remote_machine
    rows = temp_db.fetchone(
        "SELECT COUNT(*) AS n FROM sessions WHERE external_id LIKE 'foreign-%'", ()
    )
    assert rows is not None and int(rows["n"]) == 0


async def test_workspace_intent_names_run_branch(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
) -> None:
    before = _row_counts(temp_db)

    await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"], isolation="worktree"),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )
    named = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(
            remote_parent, sample_project["id"], isolation="worktree", branch_name="feat/x"
        ),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )
    inherited = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(
            remote_parent, sample_project["id"], isolation="worktree", base_branch="inherit"
        ),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    sends = [call.args[2] for call in node.send.await_args_list]
    first, second, third = sends
    assert first["workspace"]["mode"] == "worktree"
    assert first["workspace"]["branch_name"] == f"agent/{first['run_id']}"
    assert first["workspace"]["base_branch"] is None
    assert second["workspace"]["branch_name"] == "feat/x"
    assert third["workspace"]["base_branch"] is None
    assert named["success"] is True and inherited["success"] is True
    after = _row_counts(temp_db)
    assert after["worktrees"] == before["worktrees"]
    assert after["clones"] == before["clones"]


async def test_machine_ceiling_refuses_before_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_spawn, "configured_max_active_agents", lambda: 2)
    _fill_machine(temp_db, sample_project["id"], remote_machine, 2)
    before = _row_counts(temp_db)
    lease = _lease()

    refused = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"]),
        lease=lease,
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )

    assert refused["success"] is False
    assert refused["error_code"] == "machine_cap_reached"
    assert refused["stage"] == "none"
    assert _row_counts(temp_db) == before
    node.send.assert_not_awaited()
    lease.release_unattached.assert_called_once()

    # One below the ceiling: two concurrent spawns admit exactly one.
    monkeypatch.setattr(remote_spawn, "configured_max_active_agents", lambda: 2)
    temp_db.execute("DELETE FROM terminals WHERE machine_id = %s", (remote_machine,))
    _fill_machine(temp_db, sample_project["id"], remote_machine, 1)

    results = await asyncio.gather(
        *(
            spawn_remote_agent(
                temp_db,
                routed=_routed(remote_machine),
                request=_request(remote_parent, sample_project["id"]),
                lease=_lease(),
                daemon_config=DaemonConfig(),
                runner=runner,
                session_manager=sessions,
            )
            for _ in range(2)
        )
    )
    admitted = [result for result in results if result["success"] is True]
    refused_twice = [
        result for result in results if result.get("error_code") == "machine_cap_reached"
    ]
    assert len(admitted) == 1
    assert len(refused_twice) == 1


async def test_remote_resolvers_skip_hub_srt_install(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    remote_parent: str,
    node: AsyncMock,
    runner: MagicMock,
    sessions: ChildSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _srt_missing() -> None:
        raise AssertionError("the hub verified SRT for a remote run")

    monkeypatch.setattr(srt_runtime, "verify_srt_installation", _srt_missing)

    sent = await spawn_remote_agent(
        temp_db,
        routed=_routed(remote_machine),
        request=_request(remote_parent, sample_project["id"]),
        daemon_config=DaemonConfig(),
        runner=runner,
        session_manager=sessions,
    )
    assert sent["success"] is True
    node.send.assert_awaited_once()
    (_, _, payload), _ = node.send.await_args
    assert payload["sandbox"]["enabled"] is True
    assert payload["sandbox"]["backend"] == "srt"

    node.send.reset_mock()
    for sandbox in ({"enabled": False}, {"backend": "provider-native"}):
        refused = await spawn_remote_agent(
            temp_db,
            routed=_routed(remote_machine),
            request=_request(remote_parent, sample_project["id"]),
            daemon_config=DaemonConfig(agent_sandbox=sandbox),
            runner=runner,
            session_manager=sessions,
        )
        assert refused["success"] is False
        assert refused["error_code"] == "sandbox_required"
        assert refused["stage"] == "none"
    node.send.assert_not_awaited()


async def test_spawn_needs_owned_machine(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    local_parent: str,
    remote_machine: str,
    node: AsyncMock,
    spawn_registry: Any,
    runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    task = LocalTaskManager(temp_db).create_task(
        title="Remote spawn",
        task_type="task",
        project_id=sample_project["id"],
        validation_criteria="Spawn is observable.",
    )
    args = {"prompt": "x", "parent_session_id": local_parent, "machine": remote_machine}

    token = bind_caller(OTHER_USER_ID, temp_db)
    try:
        refused = await spawn_registry.call("spawn_agent", args)
        local_refused = await spawn_registry.call(
            "spawn_agent", {"prompt": "x", "parent_session_id": local_parent}
        )
    finally:
        reset_caller(token)
    assert refused["error_code"] == "machine_not_owned"
    assert local_refused["error_code"] == "machine_not_owned"
    node.send.assert_not_awaited()
    impl.assert_not_awaited()

    token = bind_caller(TEST_USER_ID, temp_db)
    try:
        owned = await spawn_registry.call("spawn_agent", args)
    finally:
        reset_caller(token)
    assert owned["success"] is True
    assert node.send.await_count == 1

    unbound = await spawn_registry.call("spawn_agent", args)
    assert unbound["success"] is True
    assert node.send.await_count == 2

    body = {"task_id": task.id, "prompt": "x", "machine": remote_machine, "provider": "claude"}
    with _owner_http_client(temp_db, OTHER_USER_ID, runner) as client:
        refused_http = client.post("/api/agents/spawn", json=body)
    assert refused_http.json()["error_code"] == "machine_not_owned"
    assert node.send.await_count == 2

    with _owner_http_client(temp_db, TEST_USER_ID, runner) as client:
        owned_http = client.post("/api/agents/spawn", json=body)
    assert owned_http.json()["success"] is True
    assert node.send.await_count == 3


async def test_database_free_routing_refuses_remote_target(
    local_parent: str,
    remote_parent: str,
    remote_machine: str,
    temp_db: HubDatabase,
    runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    impl = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(factory, "spawn_agent_impl", impl)
    registry = create_spawn_agent_registry(
        runner,
        session_manager=SessionManager(temp_db),
        config_resolver=lambda: DaemonConfig(),
    )

    by_machine = await registry.call(
        "spawn_agent",
        {"prompt": "x", "parent_session_id": local_parent, "machine": remote_machine},
    )
    by_resume = await registry.call(
        "spawn_agent",
        {"prompt": "x", "parent_session_id": local_parent, "resume_session_id": remote_parent},
    )

    assert by_machine["error_code"] == "routing_unavailable"
    assert by_resume["error_code"] == "routing_unavailable"
    impl.assert_not_awaited()
