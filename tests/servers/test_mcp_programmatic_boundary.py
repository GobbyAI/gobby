"""Agent MCP calls enforce rules while operator calls retain their own boundary."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.config.values import ConfigValuesService
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.config import create_config_registry
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
)
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.pipeline.handlers import execute_mcp_step
from gobby.workflows.pipeline_models import MCPStepConfig, PipelineStep
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import install_isolated_checkout_project
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.integration
BRIDGE_HEADERS = {MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION}
SERVER = "gobby-boundary"


@dataclass
class EchoTarget:
    calls: list[dict[str, Any]]
    failing: bool = False

    def echo(self, value: str, session_id: str) -> dict[str, Any]:
        self.calls.append(
            {
                "value": value,
                "session_id": session_id,
                "context": get_current_session_id(),
                "project": get_project_context(),
            }
        )
        if self.failing:
            return {"success": False, "error": "target failed"}
        return {"value": value, "session_id": session_id}


@dataclass
class Boundary:
    client: TestClient
    http_server: HTTPServer
    agent: bool
    proxy: ToolProxyService
    variables: SessionVariableManager
    hooks: WorkflowHookHandler
    headers: dict[str, str]
    session_id: str
    project_id: str
    target: EchoTarget
    message_calls: list[str]
    config_patches: list[tuple[int, dict[str, object], list[str]]]
    metrics: MagicMock

    def call(self, route: str, arguments: Any, *, bridge: bool = False) -> dict[str, Any]:
        body = (
            {"server_name": SERVER, "tool_name": "echo", "arguments": arguments}
            if route == "/api/mcp/tools/call"
            else arguments
        )
        response = self.client.post(
            route, json=body, headers=self.headers | (BRIDGE_HEADERS if bridge else {})
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert isinstance(result, dict)
        return result


@pytest.fixture(params=["operator", "agent"])
def boundary(
    request: pytest.FixtureRequest,
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Boundary]:
    machine_id = "21000000-0000-4000-8000-000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / "checkout", machine_id=machine_id
    )
    project_id = checkout.project.id
    sessions = SessionManager(temp_db)
    session = sessions.register(
        external_id="programmatic-boundary",
        machine_id=machine_id,
        source="codex",
        project_id=project_id,
        agent_depth=1,
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        child_session_id=session.id,
        provider="codex",
        prompt="isolated boundary test",
    )
    sessions.update_terminal_pickup_metadata(session.id, agent_run_id=run.id)
    variables = SessionVariableManager(temp_db)
    rules = RuleDefinitionManager(temp_db)
    rules.create(
        name="boundary-schema-gate",
        definition_json={
            "event": "before_tool",
            "when": "tool_input.get('server_name') == 'gobby-boundary' and 'gobby-boundary:echo' not in variables.get('unlocked_tools', [])",
            "effects": [{"type": "block", "reason": "Fetch the echo schema first"}],
        },
        priority=1,
    )
    rules.create(
        name="boundary-message-gate",
        definition_json={
            "event": "before_tool",
            "when": "tool_input.get('server_name') == 'gobby-agents' and tool_input.get('tool_name') == 'send_message'",
            "effects": [{"type": "block", "reason": "Messaging blocked by workflow"}],
        },
        priority=1,
    )
    for event in ("before_tool", "after_tool"):
        rules.create(
            name=f"boundary-{event}",
            definition_json={
                "event": event,
                "effects": [{"type": "set_variable", "variable": f"saw_{event}", "value": True}],
            },
        )
    hooks = WorkflowHookHandler(
        rule_engine=RuleEngine(temp_db),
        enabled=True,
        evaluation_runtime=WorkflowEvaluationRuntime(),
    )
    hook_manager = cast(
        HookManager,
        SimpleNamespace(_database=temp_db, _session_manager=sessions, _workflow_handler=hooks),
    )
    target = EchoTarget([])
    registry = InternalToolRegistry(SERVER)
    registry.register(
        "echo",
        "Echo a value with caller attribution",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}, "session_id": {"type": "string"}},
            "required": ["value", "session_id"],
        },
        target.echo,
    )
    internal = InternalRegistryManager()
    internal.add_registry(registry)
    message_calls: list[str] = []

    def send_message(content: str) -> dict[str, Any]:
        message_calls.append(content)
        return {"success": True}

    messages = InternalToolRegistry("gobby-agents")
    messages.register(
        "send_message",
        "Record an isolated message",
        {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
        send_message,
    )
    internal.add_registry(messages)
    config_patches: list[tuple[int, dict[str, object], list[str]]] = []

    async def record_config_patch(
        *, expected_revision: int, values: Mapping[str, object], unset: Collection[str]
    ) -> dict[str, object]:
        config_patches.append((expected_revision, dict(values), list(unset)))
        return {"committed": True, "revision": expected_revision + 1}

    config_service = cast(ConfigValuesService, SimpleNamespace(patch=record_config_patch))
    internal.add_registry(create_config_registry(lambda: config_service))
    metrics = MagicMock()
    manager = MagicMock(spec=MCPClientManager)
    manager.session_manager = sessions
    manager.project_id = project_id
    manager.metrics_manager = metrics
    proxy = ToolProxyService(
        manager, internal_manager=internal, hook_manager_resolver=lambda: hook_manager
    )
    server = create_http_server(
        database=temp_db,
        session_manager=sessions,
        project_id=project_id,
        authenticated_requests=False,
    )
    server.app.state.server = server
    server._internal_manager = internal
    server._tools_handler = cast(GobbyDaemonTools, SimpleNamespace(tool_proxy=proxy))
    token_file = tmp_path / "api-token"
    token_file.write_text("boundary-operator-token")
    AuthStore(temp_db).set_local_api_token_hash(hash_token("boundary-operator-token"))
    server.auth_service = AuthService(
        lambda: temp_db, token_file=token_file, bootstrap_file=_managed_bootstrap(token_file)
    )
    token = "boundary-operator-token"
    if request.param == "agent":
        token = issue_agent_api_token(
            derive_managed_signing_key(token),
            agent_run_id=run.id,
            session_id=session.id,
            project_id=project_id,
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Session-Id": session.id,
        "X-Gobby-Project-Id": project_id,
        "X-Gobby-Agent-Run-Id": run.id,
    }
    client = TestClient(server.app)
    try:
        yield Boundary(
            client,
            server,
            request.param == "agent",
            proxy,
            variables,
            hooks,
            headers,
            session.id,
            project_id,
            target,
            message_calls,
            config_patches,
            metrics,
        )
    finally:
        client.close()
        hooks.shutdown()


@pytest.mark.parametrize("boundary", ["operator"], indirect=True)
@pytest.mark.parametrize("route", ["/api/mcp/tools/call", f"/api/mcp/{SERVER}/tools/echo"])
def test_programmatic_calls_skip_rules_and_preserve_attribution(
    boundary: Boundary, route: str
) -> None:
    before = boundary.variables.get_variables(boundary.session_id)
    result = boundary.call(route, {"value": "hello"})
    assert result["success"] is True
    assert result["result"] == {"value": "hello", "session_id": boundary.session_id}
    assert boundary.target.calls[0]["context"] == boundary.session_id
    assert boundary.target.calls[0]["project"]["id"] == boundary.project_id
    assert boundary.variables.get_variables(boundary.session_id) == before
    metric = boundary.metrics.record_call.call_args.kwargs
    assert metric["session_id"] == boundary.session_id
    assert metric["project_id"] == boundary.project_id
    assert metric["success"] is True

    blocked = boundary.call(route, {"value": "hello"}, bridge=True)
    assert blocked["success"] is False
    assert blocked["error_code"] == "TOOL_BLOCKED"
    assert "Fetch the echo schema" in blocked["error"]
    assert len(boundary.target.calls) == 1
    schema = boundary.client.post(
        "/api/mcp/tools/schema",
        headers=boundary.headers | BRIDGE_HEADERS,
        json={"server_name": SERVER, "tool_name": "echo"},
    )
    assert schema.status_code == 200
    assert schema.json()["inputSchema"]["required"] == ["value", "session_id"]
    allowed = boundary.call(route, {"value": "hello"}, bridge=True)
    assert allowed["success"] is True
    state = boundary.variables.get_variables(boundary.session_id)
    assert state["saw_before_tool"] is True
    assert state["saw_after_tool"] is True


@pytest.mark.parametrize("boundary", ["agent"], indirect=True)
@pytest.mark.parametrize("route", ["/api/mcp/tools/call", f"/api/mcp/{SERVER}/tools/echo"])
def test_unmarked_agent_call_obeys_workflow_rules(boundary: Boundary, route: str) -> None:
    listed = boundary.client.get(f"/api/mcp/{SERVER}/tools", headers=boundary.headers)
    assert listed.status_code == 200
    assert SERVER in boundary.variables.get_variables(boundary.session_id)["listed_servers"]

    blocked = boundary.call(route, {"value": "hello"})
    assert blocked["success"] is False
    assert blocked["error_code"] == "TOOL_BLOCKED"
    assert "Fetch the echo schema" in blocked["error"]
    assert boundary.target.calls == []

    schema = boundary.client.post(
        "/api/mcp/tools/schema",
        headers=boundary.headers,
        json={"server_name": SERVER, "tool_name": "echo"},
    )
    assert schema.status_code == 200
    assert schema.json()["inputSchema"]["required"] == ["value", "session_id"]
    with patch.object(
        boundary.proxy,
        "_apply_after_tool_workflow",
        wraps=boundary.proxy._apply_after_tool_workflow,
    ) as after_tool:
        allowed = boundary.call(route, {"value": "hello"})
    assert after_tool.await_count == 1
    assert allowed["success"] is True
    assert allowed["result"] == {"value": "hello", "session_id": boundary.session_id}
    state = boundary.variables.get_variables(boundary.session_id)
    assert state["saw_before_tool"] is True
    assert state["saw_after_tool"] is True


@pytest.mark.parametrize("boundary", ["agent"], indirect=True)
@pytest.mark.parametrize("route", ["/api/mcp/tools/call", f"/api/mcp/{SERVER}/tools/echo"])
def test_unmarked_agent_failure_updates_error_bookkeeping(boundary: Boundary, route: str) -> None:
    schema = boundary.client.post(
        "/api/mcp/tools/schema",
        headers=boundary.headers,
        json={"server_name": SERVER, "tool_name": "echo"},
    )
    assert schema.status_code == 200
    boundary.target.failing = True
    failed = boundary.call(route, {"value": "hello"})
    assert failed["success"] is False
    assert "target failed" in failed["error"]
    state = boundary.variables.get_variables(boundary.session_id)
    assert len(state["open_tool_errors"]) == 1
    assert state["saw_after_tool"] is True


@pytest.mark.parametrize("boundary", ["agent"], indirect=True)
@pytest.mark.parametrize("route", ["/api/mcp/tools/call", f"/api/mcp/{SERVER}/tools/echo"])
def test_unmarked_agent_call_requires_bound_session(boundary: Boundary, route: str) -> None:
    body: dict[str, Any] = {"value": "hello"}
    if route == "/api/mcp/tools/call":
        body = {"server_name": SERVER, "tool_name": "echo", "arguments": body}

    missing = boundary.headers.copy()
    del missing["X-Gobby-Session-Id"]
    response = boundary.client.post(route, json=body, headers=missing)
    assert response.status_code == 401

    mismatched = boundary.headers | {"X-Gobby-Session-Id": "00000000-0000-4000-8000-000000000002"}
    response = boundary.client.post(route, json=body, headers=mismatched)
    assert response.status_code == 401
    assert boundary.target.calls == []


@pytest.mark.parametrize("boundary", ["agent"], indirect=True)
@pytest.mark.parametrize("route", ["/api/mcp/tools/call", f"/api/mcp/{SERVER}/tools/echo"])
def test_marked_agent_call_emits_one_after_tool(boundary: Boundary, route: str) -> None:
    schema = boundary.client.post(
        "/api/mcp/tools/schema",
        headers=boundary.headers | BRIDGE_HEADERS,
        json={"server_name": SERVER, "tool_name": "echo"},
    )
    assert schema.status_code == 200
    with patch.object(
        boundary.proxy,
        "_apply_after_tool_workflow",
        wraps=boundary.proxy._apply_after_tool_workflow,
    ) as after_tool:
        allowed = boundary.call(route, {"value": "hello"}, bridge=True)
    assert allowed["success"] is True
    assert after_tool.await_count == 1


@pytest.mark.parametrize("boundary", ["agent"], indirect=True)
@pytest.mark.parametrize(
    "route", ["/api/mcp/tools/call", "/api/mcp/gobby-agents/tools/send_message"]
)
@pytest.mark.parametrize("bridge", [False, True])
def test_agent_message_rule_blocks_marked_and_unmarked_calls(
    boundary: Boundary, route: str, bridge: bool
) -> None:
    body: dict[str, Any] = {"content": "isolated message"}
    if route == "/api/mcp/tools/call":
        body = {"server_name": "gobby-agents", "tool_name": "send_message", "arguments": body}
    headers = boundary.headers | (BRIDGE_HEADERS if bridge else {})
    response = boundary.client.post(route, json=body, headers=headers)
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is False
    assert result["error_code"] == "TOOL_BLOCKED"
    assert "Messaging blocked by workflow" in result["error"]
    assert boundary.message_calls == []


@pytest.mark.parametrize("boundary", ["operator"], indirect=True)
@pytest.mark.parametrize(
    "route", ["/api/mcp/tools/call", "/api/mcp/gobby-agents/tools/send_message"]
)
def test_operator_message_call_retains_programmatic_path(boundary: Boundary, route: str) -> None:
    body: dict[str, Any] = {"content": "isolated message"}
    if route == "/api/mcp/tools/call":
        body = {"server_name": "gobby-agents", "tool_name": "send_message", "arguments": body}
    response = boundary.client.post(route, json=body, headers=boundary.headers)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert boundary.message_calls == ["isolated message"]


@pytest.mark.parametrize("boundary", ["agent", "operator"], indirect=True)
@pytest.mark.parametrize(
    "route", ["/api/mcp/tools/call", "/api/mcp/gobby-config/tools/patch_config_values"]
)
def test_config_patch_requires_operator_authority(boundary: Boundary, route: str) -> None:
    if boundary.agent:
        schema = boundary.client.post(
            "/api/mcp/tools/schema",
            json={"server_name": "gobby-config", "tool_name": "patch_config_values"},
            headers=boundary.headers,
        )
        assert schema.status_code == 200
    patch_args = {
        "expected_revision": 4,
        "values": {
            "agent_sandbox": {"enabled": False},
            "rules": {"enforcement_enabled": False},
        },
    }
    body = (
        {"server_name": "gobby-config", "tool_name": "patch_config_values", "arguments": patch_args}
        if route == "/api/mcp/tools/call"
        else patch_args
    )
    response = boundary.client.post(route, json=body, headers=boundary.headers)
    assert response.status_code == 200
    result = response.json()
    if boundary.agent:
        assert result["result"]["error"]["code"] == "forbidden"
        assert boundary.config_patches == []
    else:
        assert result["success"] is True
        assert boundary.config_patches == [(4, patch_args["values"], [])]


@pytest.mark.parametrize("boundary", ["agent"], indirect=True)
def test_agent_token_cannot_patch_config_through_rest(boundary: Boundary) -> None:
    response = boundary.client.patch(
        "/api/config/values",
        json={"expected_revision": 4, "values": {"agent_sandbox": {"enabled": False}}},
        headers=boundary.headers,
    )
    assert response.status_code == 401
    assert boundary.config_patches == []


@pytest.mark.parametrize("boundary", ["agent", "operator"], indirect=True)
@pytest.mark.parametrize(
    "route", ["/api/mcp/tools/call", "/api/mcp/gobby-agents/tools/send_message"]
)
def test_proxy_unavailable_refuses_agent_but_preserves_operator_fallback(
    boundary: Boundary, route: str
) -> None:
    boundary.http_server._tools_handler = cast(GobbyDaemonTools, SimpleNamespace(tool_proxy=None))
    body: dict[str, Any] = {"content": "isolated message"}
    if route == "/api/mcp/tools/call":
        body = {"server_name": "gobby-agents", "tool_name": "send_message", "arguments": body}
    response = boundary.client.post(route, json=body, headers=boundary.headers)
    if boundary.agent:
        assert response.status_code == 503
        assert boundary.message_calls == []
    else:
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert boundary.message_calls == ["isolated message"]


@pytest.mark.parametrize("boundary", ["operator"], indirect=True)
@pytest.mark.parametrize("route", ["/api/mcp/tools/call", f"/api/mcp/{SERVER}/tools/echo"])
def test_programmatic_errors_and_discovery_leave_agent_state_untouched(
    boundary: Boundary, route: str
) -> None:
    initial = boundary.variables.get_variables(boundary.session_id)
    boundary.target.failing = True
    failed = boundary.call(route, {"value": "hello"})
    assert failed["success"] is False
    assert "target failed" in failed["error"]
    invalid = boundary.call(route, {})
    assert invalid["error_code"] == "INVALID_ARGUMENTS"
    assert invalid["schema"]["required"] == ["value", "session_id"]
    schema = boundary.client.post(
        "/api/mcp/tools/schema",
        headers=boundary.headers,
        json={"server_name": SERVER, "tool_name": "echo"},
    )
    assert schema.status_code == 200
    listed = boundary.client.get(f"/api/mcp/{SERVER}/tools", headers=boundary.headers)
    assert listed.status_code == 200
    assert boundary.variables.get_variables(boundary.session_id) == initial

    schema = boundary.client.post(
        "/api/mcp/tools/schema",
        headers=boundary.headers | BRIDGE_HEADERS,
        json={"server_name": SERVER, "tool_name": "echo"},
    )
    assert schema.status_code == 200
    agent_failure = boundary.call(route, {"value": "hello"}, bridge=True)
    assert agent_failure["success"] is False
    state = boundary.variables.get_variables(boundary.session_id)
    assert len(state["open_tool_errors"]) == 1
    # Same target identity: a programmatic success must not clear an agent's error.
    boundary.target.failing = False
    assert boundary.call(route, {"value": "hello"})["success"] is True
    invalid = boundary.call(route, {})
    assert invalid["error_code"] == "INVALID_ARGUMENTS"
    assert invalid["schema"]["required"] == ["value", "session_id"]
    schema = boundary.client.post(
        "/api/mcp/tools/schema",
        headers=boundary.headers,
        json={"server_name": SERVER, "tool_name": "echo"},
    )
    assert schema.status_code == 200
    listed = boundary.client.get(f"/api/mcp/{SERVER}/tools", headers=boundary.headers)
    assert listed.status_code == 200
    assert boundary.variables.get_variables(boundary.session_id) == state


@pytest.mark.parametrize("route", ["/api/mcp/tools/call", f"/api/mcp/{SERVER}/tools/echo"])
def test_programmatic_calls_still_reject_bad_auth_and_identity(
    boundary: Boundary, route: str
) -> None:
    body: dict[str, Any] = {"value": "hello"}
    if route == "/api/mcp/tools/call":
        body = {"server_name": SERVER, "tool_name": "echo", "arguments": body}
    invalid_auth = boundary.client.post(
        route, json=body, headers=boundary.headers | {"Authorization": "Bearer invalid"}
    )
    assert invalid_auth.status_code == 401
    invalid_run = boundary.client.post(
        route, json=body, headers=boundary.headers | {"X-Gobby-Agent-Run-Id": "invalid"}
    )
    assert invalid_run.status_code in {401, 403}
    assert boundary.target.calls == []


@pytest.mark.asyncio
async def test_shell_rule_can_deny_agent_cli_access(boundary: Boundary) -> None:
    RuleDefinitionManager(boundary.variables.db).create(
        name="deny-gobby-cli",
        definition_json={
            "event": "before_tool",
            "when": "event.data.get('tool_name') == 'Bash' and 'gobby mcp-proxy' in tool_input.get('command', '')",
            "effects": [{"type": "block", "reason": "CLI access denied"}],
        },
    )
    response = await boundary.hooks.evaluate_async(
        HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            session_id=boundary.session_id,
            metadata={"_platform_session_id": boundary.session_id},
            project_id=boundary.project_id,
            data={"tool_name": "Bash", "tool_input": {"command": "gobby mcp-proxy call-tool"}},
        )
    )
    assert response.decision == "block"
    assert response.reason == "Rule enforced by Gobby: [deny-gobby-cli]\nCLI access denied"


@pytest.mark.asyncio
async def test_pipeline_calls_need_no_discovery_and_keep_session(boundary: Boundary) -> None:
    before = boundary.variables.get_variables(boundary.session_id)
    step = PipelineStep(
        id="echo", mcp=MCPStepConfig(server=SERVER, tool="echo", arguments={"value": "pipeline"})
    )
    result = await execute_mcp_step(
        step,
        {"session_id": boundary.session_id, "project_id": boundary.project_id},
        lambda: boundary.proxy,
        SessionManager(boundary.variables.db),
    )
    assert result == {"value": "pipeline", "session_id": boundary.session_id}
    assert boundary.target.calls[0]["context"] == boundary.session_id
    listed = await boundary.proxy.list_tools(SERVER, session_id=boundary.session_id)
    assert listed["tools"][0]["name"] == "echo"
    assert boundary.variables.get_variables(boundary.session_id) == before


def _managed_bootstrap(token_file: Path) -> Path:
    bootstrap = token_file.with_name(token_file.name + ".bootstrap.yaml")
    api_key = token_file.read_text().strip() if token_file.exists() else None
    bootstrap.write_text(json.dumps({"api_key": api_key}))
    return bootstrap
