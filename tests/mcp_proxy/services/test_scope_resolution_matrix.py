"""Scope-resolution matrix for the MCP proxy front door (plan 4.2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import ConnectionState, MCPServerConfig
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.services.server_resolution import (
    ProjectScopeUnresolvedError,
    resolve_request_scope,
)
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.servers.routes.mcp.endpoints.execution import call_mcp_tool
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.utils.session_context import SeededContextTokens

pytestmark = pytest.mark.unit

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_PROJECT_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_SERVER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GLOBAL_SERVER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
FOREIGN_SERVER_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class RecordingManager:
    """Id-keyed manager that records every boundary call."""

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        project_id: str | None = None,
    ) -> None:
        self._configs = {config.id: config for config in configs}
        self.project_id = project_id
        self.lazy_connect = True
        self.calls: list[tuple[Any, ...]] = []
        self.health: dict[str, Any] = {}
        for config in configs:
            self.health[config.id] = SimpleNamespace(
                state=ConnectionState.CONNECTED if config.enabled else ConnectionState.DISABLED,
            )

    @property
    def server_configs(self) -> list[MCPServerConfig]:
        return list(self._configs.values())

    def has_server(self, server_id: str) -> bool:
        self.calls.append(("has_server", server_id))
        return server_id in self._configs

    def get_server_config(self, server_id: str) -> MCPServerConfig | None:
        self.calls.append(("get_server_config", server_id))
        return self._configs.get(server_id)

    def is_connected(self, server_id: str) -> bool:
        config = self._configs.get(server_id)
        return bool(config is not None and config.enabled)

    async def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(("call_tool", server_id, tool_name, arguments))
        config = self._configs[server_id]
        return {
            "success": True,
            "id": server_id,
            "name": config.name,
            "env": dict(config.env or {}),
        }

    async def list_tools(
        self,
        server_id: str | None = None,
        *,
        project_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        self.calls.append(("list_tools", server_id, project_id))
        if server_id is None:
            return {}
        config = self._configs[server_id]
        return {config.name: [{"name": "ping", "description": f"from {server_id}"}]}

    async def get_tool_input_schema(self, server_id: str, tool_name: str) -> dict[str, Any]:
        self.calls.append(("get_tool_input_schema", server_id, tool_name))
        return {"type": "object", "properties": {}}

    async def read_resource(self, server_id: str, uri: str) -> dict[str, Any]:
        self.calls.append(("read_resource", server_id, uri))
        return {"uri": uri, "id": server_id}

    async def get_client_session(self, server_id: str) -> Any:
        self.calls.append(("get_client_session", server_id))
        session = MagicMock()
        session.call_tool = AsyncMock(return_value={"id": server_id})
        return session

    async def ensure_connected(self, server_id: str) -> Any:
        return await self.get_client_session(server_id)

    def method_ids(self, method: str) -> list[str]:
        return [str(call[1]) for call in self.calls if call and call[0] == method]


def scoped_github_configs(
    *,
    include_project: bool = True,
    project_enabled: bool = True,
    include_global: bool = True,
    include_foreign: bool = True,
) -> list[MCPServerConfig]:
    configs: list[MCPServerConfig] = []
    if include_project:
        configs.append(
            MCPServerConfig(
                name="github",
                project_id=PROJECT_ID,
                url="https://project.example.test",
                id=PROJECT_SERVER_ID,
                enabled=project_enabled,
                env={"TOKEN": "project-secret"},
            )
        )
    if include_global:
        configs.append(
            MCPServerConfig(
                name="github",
                project_id=GLOBAL_PROJECT_ID,
                url="https://global.example.test",
                id=GLOBAL_SERVER_ID,
                enabled=True,
                env={"TOKEN": "global-secret"},
            )
        )
    if include_foreign:
        configs.append(
            MCPServerConfig(
                name="github",
                project_id=OTHER_PROJECT_ID,
                url="https://foreign.example.test",
                id=FOREIGN_SERVER_ID,
                enabled=True,
                env={"TOKEN": "foreign-secret"},
            )
        )
    return configs


def as_mcp(manager: RecordingManager) -> MCPClientManager:
    return cast(MCPClientManager, manager)


def make_proxy(manager: RecordingManager) -> ToolProxyService:
    internal = MagicMock()
    internal.is_internal.return_value = False
    return ToolProxyService(
        mcp_manager=as_mcp(manager),
        internal_manager=internal,
        validate_arguments=False,
    )


async def _drive(proxy: ToolProxyService, server_name: str, operation: str) -> Any:
    if operation == "call_tool":
        return await proxy.call_tool(server_name, "ping", {})
    if operation == "list_tools":
        return await proxy.list_tools(server_name)
    if operation == "get_tool_schema":
        return await proxy.get_tool_schema(server_name, "ping")
    raise AssertionError(f"unknown operation {operation}")


@pytest.mark.asyncio
async def test_scope_resolution_matrix() -> None:
    operations = ("call_tool", "list_tools", "get_tool_schema")

    for operation in operations:
        manager = RecordingManager(
            scoped_github_configs(),
            project_id=PROJECT_ID,
        )
        result = await _drive(make_proxy(manager), "github", operation)
        assert result.get("success") is True
        dispatched = manager.method_ids(operation) or manager.method_ids("get_tool_input_schema")
        assert PROJECT_SERVER_ID in dispatched
        assert GLOBAL_SERVER_ID not in dispatched
        assert FOREIGN_SERVER_ID not in dispatched

        fallback = RecordingManager(
            scoped_github_configs(include_project=False),
            project_id=PROJECT_ID,
        )
        result = await _drive(make_proxy(fallback), "github", operation)
        assert result.get("success") is True
        dispatched = fallback.method_ids(operation) or fallback.method_ids("get_tool_input_schema")
        assert GLOBAL_SERVER_ID in dispatched
        assert FOREIGN_SERVER_ID not in dispatched

        disabled = RecordingManager(
            scoped_github_configs(project_enabled=False),
            project_id=PROJECT_ID,
        )
        await _drive(make_proxy(disabled), "github", operation)
        dispatched = disabled.method_ids(operation) or disabled.method_ids("get_tool_input_schema")
        assert GLOBAL_SERVER_ID not in dispatched
        assert "global-secret" not in repr(disabled.calls)

        foreign = RecordingManager(
            scoped_github_configs(),
            project_id=PROJECT_ID,
        )
        result = await _drive(make_proxy(foreign), FOREIGN_SERVER_ID, operation)
        assert result.get("success") is False
        dispatched = foreign.method_ids(operation) or foreign.method_ids("get_tool_input_schema")
        assert FOREIGN_SERVER_ID not in dispatched
        assert PROJECT_SERVER_ID not in dispatched
        assert GLOBAL_SERVER_ID not in dispatched

        revealed = RecordingManager(
            scoped_github_configs(include_project=False),
            project_id=PROJECT_ID,
        )
        result = await _drive(make_proxy(revealed), "github", operation)
        assert result.get("success") is True
        dispatched = revealed.method_ids(operation) or revealed.method_ids("get_tool_input_schema")
        assert GLOBAL_SERVER_ID in dispatched


@pytest.mark.asyncio
async def test_resolve_request_scope_is_total_over_explicit_inputs() -> None:
    fallback = "fallback-project"
    exists = {"known-project": True}

    def project_exists(project_id: str) -> bool:
        return exists.get(project_id, False)

    empty = resolve_request_scope(
        session_project_id=None,
        project_id=None,
        scope=None,
        fallback_project_id=fallback,
        project_exists=project_exists,
    )
    assert empty == fallback

    http_empty = resolve_request_scope(
        session_project_id=None,
        project_id="",
        scope=None,
        fallback_project_id=GLOBAL_PROJECT_ID,
        project_exists=project_exists,
    )
    mcp_empty = resolve_request_scope(
        session_project_id=None,
        project_id=None,
        scope=None,
        fallback_project_id=fallback,
        project_exists=project_exists,
    )
    assert http_empty == GLOBAL_PROJECT_ID
    assert mcp_empty == fallback

    assert (
        resolve_request_scope(
            session_project_id=PROJECT_ID,
            project_id="known-project",
            scope="global",
            fallback_project_id=fallback,
            project_exists=project_exists,
        )
        == GLOBAL_PROJECT_ID
    )
    assert (
        resolve_request_scope(
            session_project_id=PROJECT_ID,
            project_id="known-project",
            scope=None,
            fallback_project_id=fallback,
            project_exists=project_exists,
        )
        == PROJECT_ID
    )
    assert (
        resolve_request_scope(
            session_project_id=None,
            project_id="known-project",
            scope=None,
            fallback_project_id=fallback,
            project_exists=project_exists,
        )
        == "known-project"
    )

    with pytest.raises(ProjectScopeUnresolvedError):
        resolve_request_scope(
            session_project_id=None,
            project_id="missing-project",
            scope=None,
            fallback_project_id=fallback,
            project_exists=project_exists,
        )
    with pytest.raises(ProjectScopeUnresolvedError):
        resolve_request_scope(
            session_project_id=None,
            project_id=None,
            scope="project",
            fallback_project_id=fallback,
            project_exists=project_exists,
        )

    mcp_fallbacks: list[str] = []
    http_fallbacks: list[str] = []
    real = resolve_request_scope

    def mcp_spy(**kwargs: Any) -> str:
        mcp_fallbacks.append(str(kwargs["fallback_project_id"]))
        return real(**kwargs)

    def http_spy(**kwargs: Any) -> str:
        http_fallbacks.append(str(kwargs["fallback_project_id"]))
        return real(**kwargs)

    tools = GobbyDaemonTools(
        mcp_manager=as_mcp(RecordingManager([], project_id=None)),
        daemon_port=1,
        websocket_port=2,
        start_time=0.0,
        internal_manager=MagicMock(),
        db=None,
    )
    with (
        patch("gobby.utils.project_context.get_project_context", return_value={"id": fallback}),
        patch(
            "gobby.mcp_proxy.services.server_resolution.resolve_request_scope",
            side_effect=mcp_spy,
        ),
    ):
        await tools.list_mcp_servers()
    assert fallback in mcp_fallbacks

    request = MagicMock()
    request.json = AsyncMock(
        return_value={"server_name": "github", "tool_name": "ping", "arguments": {}}
    )
    request.headers = {}
    http_server = MagicMock()
    http_server.tool_proxy = MagicMock()
    http_server.tool_proxy.call_tool = AsyncMock(return_value={"success": True})
    http_server._internal_manager = None
    http_server.mcp_manager = None
    with (
        patch(
            "gobby.servers.routes.mcp.endpoints.request_context._set_context_for_request",
            AsyncMock(return_value=SeededContextTokens()),
        ),
        patch(
            "gobby.servers.routes.mcp.endpoints.execution.resolve_request_scope",
            side_effect=http_spy,
        ),
    ):
        await call_mcp_tool(request, http_server)
    assert http_fallbacks == [GLOBAL_PROJECT_ID]
    assert mcp_fallbacks != http_fallbacks


# tdd-pin: 4.2 named module-level nodeids for close-gate red/green evidence
