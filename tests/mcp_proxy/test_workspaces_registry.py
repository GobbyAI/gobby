"""gobby-workspaces MCP registry (plan gclient-workspaces 2.3)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.auth_service import AuthService
from gobby.servers.grant_auth import AuthDecision
from gobby.servers.http import HTTPServer
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import PreparedSpawn, TerminalSpawnRequest
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.write_coordinator import WriteCoordinator
from gobby.utils.local_token import (
    AgentApiTokenClaims,
    classify_agent_api_token,
    issue_agent_api_token,
)
from gobby.utils.session_context import (
    reset_request_principal,
    session_context_for_test,
    set_request_principal,
)
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"
OPERATOR_TOKEN = "workspaces-operator-token"
TOOLS = {
    "list_nodes",
    "list_workspaces",
    "get_workspace",
    "create_workspace",
    "close_workspace",
    "create_tab",
    "close_tab",
    "split_pane",
    "close_pane",
    "move_pane",
    "swap_panes",
    "rename",
    "send_text",
    "send_keys",
    "read_pane",
    "wait_for_pane_output",
}


@dataclass
class _NativeRuntime(FakeRuntime):
    """Native fake whose host epoch follows each spawn, so live locator keys stay unique."""

    backend: Literal["tmux", "native"] = "native"

    async def prepare_spawn(self, request: TerminalSpawnRequest) -> PreparedSpawn:
        self.host_epoch = str(request.terminal_id)
        return await super().prepare_spawn(request)


@dataclass
class _Stack:
    server: WebSocketServer
    sessions: SessionManager
    workspaces: WorkspaceManager
    native: _NativeRuntime
    project_id: str

    @property
    def ops(self) -> WorkspaceOps:
        assert self.server.workspace_ops is not None
        return self.server.workspace_ops


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
async def stack(temp_db: HubDatabase, sample_project: dict[str, Any]) -> AsyncIterator[_Stack]:
    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    config = MagicMock(host="localhost", port=60888, ping_interval=30, ping_timeout=10)
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), MagicMock())
    sessions = SessionManager(temp_db)
    server.session_manager = sessions
    terminals = TerminalManager(temp_db)
    native = _NativeRuntime()
    registry = runtime_registry(FakeRuntime(backend="tmux"), native)
    leases = TerminalLeaseRegistry(daemon_epoch="workspaces-registry-epoch")
    workspaces = WorkspaceManager(temp_db)
    server.configure_terminals(
        terminals,
        registry,
        lease_registry=leases,
        write_coordinator=WriteCoordinator(terminals, registry, lease_registry=leases),
        workspace_manager=workspaces,
    )
    yield _Stack(server, sessions, workspaces, native, str(sample_project["id"]))
    await leases.shutdown_lifecycle_publication()


@contextmanager
def _principal(principal: AgentApiTokenClaims | None) -> Iterator[None]:
    async def resolve() -> AgentApiTokenClaims | None:
        return principal

    token = set_request_principal(resolve)
    try:
        yield
    finally:
        reset_request_principal(token)


def _registry(stack: _Stack) -> InternalToolRegistry:
    manager = setup_internal_registries(
        config_resolver=lambda: None,
        workspace_manager=stack.workspaces,
        workspace_ops_resolver=lambda: stack.server.workspace_ops,
    )
    registry = manager.get_registry("gobby-workspaces")
    assert registry is not None
    return registry


async def _ok(registry: InternalToolRegistry, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await registry.call(tool, arguments)
    assert result["success"] is True, result
    return dict(result)


async def _code(registry: InternalToolRegistry, tool: str, **arguments: Any) -> str:
    result = await registry.call(tool, arguments)
    assert result["success"] is False, result
    assert isinstance(result["error"], str) and result["error"]
    return str(result["code"])


async def test_registry_executes_every_tool_by_ref_through_shared_ops(stack: _Stack) -> None:
    registry = _registry(stack)
    assert {tool["name"] for tool in registry.list_tools()} == TOOLS
    for name in TOOLS:
        schema = registry.get_schema(name)
        assert schema is not None
        properties = schema["inputSchema"]["properties"]
        assert properties["node"] == {"type": "string"}, name
        assert "node" not in schema["inputSchema"]["required"]
        assert "actor" not in properties
    split = registry.get_schema("split_pane")
    assert split is not None
    assert split["inputSchema"]["properties"]["axis"]["enum"] == ["horizontal", "vertical"]

    watcher = MockWebSocket()
    watcher.subscriptions = {"terminal_event"}
    stack.server.clients[watcher] = {}
    with _principal(None):
        nodes = (await _ok(registry, "list_nodes"))["nodes"]
        [local] = [row for row in nodes if row["local"]]
        assert local["id"] == LOCAL_MACHINE_ID
        assert {row["owner_user_id"] for row in nodes} == {TEST_USER_ID}
        node = f"n{local['ref']}"
        assert (await _ok(registry, "list_nodes", node=node))["nodes"] == [local]
        created = (await _ok(registry, "create_workspace", name="agents", node=node))["workspace"]
        home = f"{node}:w{created['ref']}"
        attach = {"type": "workspace_attach", "workspace": created["id"]}
        await stack.server._handle_message(watcher, json.dumps(attach))
        listed = await _ok(registry, "list_workspaces", node=node)
        assert [row["name"] for row in listed["workspaces"]] == ["agents"]

        layout = await _ok(registry, "create_tab", workspace=home, project_id=stack.project_id)
        tab, first = layout["tabs"][0], layout["panes"][0]
        tab_ref = f"{home}:t{tab['ref']}"
        first_ref = f"{tab_ref}:p{first['ref']}"
        split_layout = await _ok(registry, "split_pane", pane=first_ref, axis="vertical")
        second_ref = f"{tab_ref}:p{split_layout['panes'][0]['ref']}"
        swapped = await _ok(registry, "swap_panes", pane=first_ref, other=second_ref)
        assert swapped["tab"]["id"] == tab["id"]
        labelled = await _ok(registry, "rename", ref=first_ref, name="main")
        assert labelled["pane"]["label"] == "main"
        titled = await _ok(registry, "rename", ref=tab_ref, name="work")
        assert titled["tab"]["title"] == "work"
        renamed = await _ok(registry, "rename", ref=home, name="crew")
        assert renamed["workspace"]["name"] == "crew"
        assert await _code(registry, "rename", ref=home) == "invalid_op"

        other = await _ok(registry, "create_tab", workspace=home, project_id=stack.project_id)
        other_tab = other["tabs"][0]
        other_ref = f"{home}:t{other_tab['ref']}"
        moved = await _ok(registry, "move_pane", pane=second_ref, tab=other_ref)
        assert {row["tab_id"] for row in moved["panes"]} == {other_tab["id"]}

        written = await _ok(registry, "send_text", pane=first_ref, text="make", submit=True)
        assert written["indeterminate"] is False
        keyed = await _ok(registry, "send_keys", pane=first_ref, keys="ls\n", idempotency_key="k1")
        assert keyed["idempotency_key"] == "k1"

        snapshot = await _ok(registry, "get_workspace", workspace=home)
        assert snapshot["workspace"]["node_ref"] == local["ref"]
        assert {row["id"] for row in snapshot["tabs"]} == {tab["id"], other_tab["id"]}
        assert len(snapshot["panes"]) == 3

        closed_pane = await _ok(registry, "close_pane", pane=first_ref)
        assert [row["id"] for row in closed_pane["removed_panes"]] == [first["id"]]
        closed_tab = await _ok(registry, "close_tab", tab=other_ref)
        assert [row["id"] for row in closed_tab["removed_tabs"]] == [other_tab["id"]]
        closed = await _ok(registry, "close_workspace", workspace=home)
        assert closed["workspace"]["id"] == created["id"]

        assert await _code(registry, "close_tab", tab="w1:bogus") == "invalid_ref"
        assert await _code(registry, "list_workspaces", node="n999999") == "not_found"
        assert await _code(registry, "rename", ref=home, name="gone") == "not_found"

    # Every change went out through the WebSocket server's own broadcaster.
    kinds = {
        message.get("kind")
        for message in watcher.all_messages()
        if message["type"] == "workspace_event"
    }
    assert {"tab.created", "pane.renamed", "tab.renamed", "workspace.renamed"} <= kinds

    unconfigured = setup_internal_registries(
        config_resolver=lambda: None,
        workspace_manager=stack.workspaces,
        workspace_ops_resolver=lambda: None,
    ).get_registry("gobby-workspaces")
    assert unconfigured is not None
    with _principal(None):
        assert await _code(unconfigured, "read_pane", pane=first_ref) == "terminal_failed"


async def test_read_and_wait_address_panes_by_ref(stack: _Stack) -> None:
    registry = _registry(stack)
    with _principal(None):
        workspace = (await _ok(registry, "create_workspace", name="watch"))["workspace"]
        node = stack.workspaces.resolve_node()
        home = f"n{node.ref}:w{workspace['ref']}"
        layout = await _ok(registry, "create_tab", workspace=home, project_id=stack.project_id)
        pane = f"{home}:t{layout['tabs'][0]['ref']}:p{layout['panes'][0]['ref']}"

        stack.native.snapshot_text = "compiling\nbuild ok\n"
        screen = await _ok(registry, "read_pane", pane=pane, lines=5)
        assert screen["text"] == "compiling\nbuild ok\n"
        assert (screen["truncated"], screen["total_bytes"]) == (False, len(screen["text"]))

        # The matcher polls: the first capture misses and the next one hits.
        stack.native.snapshot_effects = ["compiling\n"]
        hit = await _ok(
            registry,
            "wait_for_pane_output",
            pane=pane,
            pattern=r"build (ok|failed)",
            timeout_seconds=5,
            poll_interval_seconds=0.1,
        )
        assert (hit["matched"], hit["reason"]) == (True, "matched")
        assert "build ok" in hit["snapshot"]["text"]

        miss = await _ok(
            registry,
            "wait_for_pane_output",
            pane=pane,
            pattern="never printed",
            timeout_seconds=0.2,
            poll_interval_seconds=0.1,
        )
        assert (miss["matched"], miss["reason"]) == (False, "timeout")
        assert miss["snapshot"]["text"] == "compiling\nbuild ok\n"

        assert await _code(registry, "read_pane", pane=f"{home}:t9:p9") == "not_found"
        assert await _code(registry, "read_pane", pane=pane, lines=0) == "invalid_op"
        bad_pattern = await _code(
            registry, "wait_for_pane_output", pane=pane, pattern="(", timeout_seconds=1
        )
        assert bad_pattern == "invalid_op"


def _http_server(stack: _Stack, temp_db: HubDatabase, tmp_path: Path) -> HTTPServer:
    services = ServiceContainer(
        database=temp_db,
        session_manager=stack.sessions,
        task_manager=MagicMock(),
        mcp_manager=MagicMock(spec=MCPClientManager),
        mcp_db_manager=None,
        workspace_manager=stack.workspaces,
        websocket_server=stack.server,
        project_id=stack.project_id,
    )
    server = HTTPServer(
        services=services,
        startup_config=DaemonConfig(),
        port=60887,
        test_mode=True,
        bootstrap_config=BootstrapConfig(),
    )
    server.app.state.server = server
    token_file = tmp_path / "local-token"
    token_file.write_text(OPERATOR_TOKEN)
    AuthStore(temp_db).set_local_api_token_hash(hash_token(OPERATOR_TOKEN))
    server.auth_service = AuthService(lambda: temp_db, token_file=token_file)
    return server


@pytest.mark.integration
async def test_actor_is_derived_from_session_context_and_principal(
    stack: _Stack, temp_db: HubDatabase, tmp_path: Path
) -> None:
    server = _http_server(stack, temp_db, tmp_path)
    session = stack.sessions.register(
        external_id="workspaces-wrapper",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=stack.project_id,
    )
    agent_session = stack.sessions.register(
        external_id="workspaces-agent",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=stack.project_id,
        agent_depth=1,
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        child_session_id=agent_session.id,
        provider="codex",
        prompt="workspaces actor test",
    )
    stack.sessions.update_terminal_pickup_metadata(agent_session.id, agent_run_id=run.id)
    agent_token = issue_agent_api_token(
        OPERATOR_TOKEN,
        agent_run_id=run.id,
        session_id=agent_session.id,
        project_id=stack.project_id,
    )
    operator = {
        "Authorization": f"Bearer {OPERATOR_TOKEN}",
        "X-Gobby-Project-Id": stack.project_id,
    }
    wrapper = operator | {
        MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
        "X-Gobby-Session-Id": session.id,
    }
    agent = {"Authorization": f"Bearer {agent_token}", "X-Gobby-Project-Id": stack.project_id}
    route = "/api/mcp/gobby-workspaces/tools/create_workspace"
    transport = httpx.ASGITransport(app=server.app)
    with patch.object(stack.ops, "workspace_create", wraps=stack.ops.workspace_create) as create:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:

            async def call(headers: dict[str, str], name: str) -> dict[str, Any]:
                response = await client.post(route, json={"name": name}, headers=headers)
                assert response.status_code == 200, response.text
                return dict(response.json())

            as_session = await call(wrapper, "from-session")
            assert as_session["success"] is True, as_session
            as_operator = await call(operator, "from-operator")
            assert as_operator["success"] is True, as_operator
            assert [awaited.args[0] for awaited in create.await_args_list] == [
                f"session:{session.id}",
                "operator",
            ]

            # Authentication already refuses an agent token that names no session;
            # past that layer, the registry still refuses the agent principal itself.
            rejected = await client.post(route, json={"name": "from-agent"}, headers=agent)
            assert rejected.status_code == 401
            allow = AuthDecision(allowed=True)
            with patch.object(server.auth_service, "authenticate", return_value=allow):
                refused = await call(agent, "from-agent")
            assert (refused["success"], refused.get("code")) == (False, "forbidden"), refused
            assert create.await_count == 2

        internal = server._internal_manager
        assert internal is not None
        registry = internal.get_registry("gobby-workspaces")
        assert registry is not None
        claims = classify_agent_api_token(agent_token, OPERATOR_TOKEN)
        assert isinstance(claims, AgentApiTokenClaims)
        with _principal(claims):
            assert await _code(registry, "create_workspace", name="claims") == "forbidden"
            with session_context_for_test(session.id):
                await _ok(registry, "create_workspace", name="claims")
        assert create.await_args_list[-1].args[0] == f"session:{session.id}"
        assert await _code(registry, "create_workspace", name="unseeded") == "forbidden"
        assert await _code(registry, "list_nodes") == "forbidden"
        assert create.await_count == 3
