"""HTTP MCP wrapper coverage for tool-result delivery shaping."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.features import ToolResultOffloadConfig
from gobby.mcp_proxy.services.result_offload import ToolResultOffloader
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.mcp_proxy.tools.results import create_results_registry
from gobby.servers.routes.mcp.endpoints.execution import call_mcp_tool, mcp_proxy
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tool_results import ToolResultStore
from tests.mcp_proxy.result_offload_test_support import TEST_MAX_ENVELOPE_CHARS

pytestmark = pytest.mark.unit


def _server(result: dict[str, Any]) -> MagicMock:
    server = MagicMock()
    server.session_manager = None
    server.config = None
    server.tool_proxy = MagicMock()
    server.tool_proxy.call_tool = AsyncMock(return_value=result)
    server._internal_manager = None
    server.mcp_manager = None
    return server


def _request(body: dict[str, Any]) -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.query_params = {}
    request.json = AsyncMock(return_value=body)
    return request


def _offload_server(db: HubDatabase) -> tuple[MagicMock, dict[str, Any]]:
    config = ToolResultOffloadConfig(
        threshold_chars=3_000,
        max_envelope_chars=TEST_MAX_ENVELOPE_CHARS,
        preview_chars=200,
        chunk_chars=200,
        max_stored_chars=10_000,
    )
    payload = {"needle": "retrievable", "content": "x" * 4_000}
    large_registry = InternalToolRegistry("gobby-large")
    large_registry.register(
        name="list_large",
        description="Return one oversized result.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: payload,
    )
    manager = InternalRegistryManager()
    manager.add_registry(large_registry)
    manager.add_registry(create_results_registry(db, config))
    offloader = ToolResultOffloader(ToolResultStore(db, config), db, config, lambda: None)
    mcp_manager = MagicMock()
    mcp_manager.project_id = None
    mcp_manager.session_manager = None
    proxy = ToolProxyService(
        mcp_manager=mcp_manager,
        internal_manager=manager,
        validate_arguments=False,
        result_offloader=offloader,
    )
    server = MagicMock()
    server.session_manager = None
    server.config = None
    server.tool_proxy = proxy
    server._internal_manager = manager
    server.mcp_manager = None
    server.services = SimpleNamespace(database=db)
    return server, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["structured", "server_tool"])
@pytest.mark.parametrize(
    "tool_result",
    [
        {
            "offloaded": True,
            "result_id": "11111111-1111-4111-8111-111111111111",
            "preview": "x" * 1_300,
        },
        {
            "result_id": "11111111-1111-4111-8111-111111111111",
            "content": "x" * 1_300,
            "offset": 0,
            "next_offset": 1_300,
            "total_chars": 4_000,
        },
    ],
)
async def test_http_wrapper_final_result_stays_within_shared_cap(
    route: str,
    tool_result: dict[str, Any],
) -> None:
    server = _server(tool_result)
    request = MagicMock()
    request.headers = {}
    request.query_params = {"intent": "wrapper-summary"}

    if route == "structured":
        request.json = AsyncMock(
            return_value={
                "server_name": "example",
                "tool_name": "large_tool",
                "arguments": {"intent": "target-value"},
                "intent": "wrapper-summary",
            }
        )
        response = await call_mcp_tool(request, server)
    else:
        request.json = AsyncMock(return_value={"intent": "target-value"})
        response = await mcp_proxy("example", "large_tool", request, server)

    assert response["success"] is True
    assert response["result"] == tool_result
    assert len(json.dumps(response, ensure_ascii=False, default=str)) <= TEST_MAX_ENVELOPE_CHARS
    call = server.tool_proxy.call_tool.await_args
    assert call.args[2] == {"intent": "target-value"}
    assert call.kwargs["wrapper_originated"] is True
    assert call.kwargs["intent"] == "wrapper-summary"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_call_route_can_bypass_result_offload(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    server, payload = _offload_server(temp_db)
    body = {
        "server_name": "gobby-large",
        "tool_name": "list_large",
        "arguments": {},
        "project_id": sample_project["id"],
    }

    inline = await call_mcp_tool(_request({**body, "offload": False}), server)
    default = await call_mcp_tool(_request(body), server)

    assert inline["result"] == payload
    assert default["result"]["offloaded"] is True
    assert default["result"]["result_id"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_body_project_context_retrieves_route_offload_without_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
) -> None:
    server, _ = _offload_server(temp_db)
    project_id = sample_project["id"]
    offloaded = await call_mcp_tool(
        _request(
            {
                "server_name": "gobby-large",
                "tool_name": "list_large",
                "arguments": {},
                "project_id": project_id,
            }
        ),
        server,
    )
    result_id = offloaded["result"]["result_id"]

    retrieved = await call_mcp_tool(
        _request(
            {
                "server_name": "gobby-results",
                "tool_name": "get_tool_result",
                "arguments": {"result_id": result_id},
                "project_id": project_id,
            }
        ),
        server,
    )
    other_project = project_manager.create(name="other-route-results-project")
    mismatched = await call_mcp_tool(
        _request(
            {
                "server_name": "gobby-results",
                "tool_name": "get_tool_result",
                "arguments": {"result_id": result_id},
                "project_id": other_project.id,
            }
        ),
        server,
    )

    assert "retrievable" in retrieved["result"]["content"]
    assert mismatched["success"] is False
    assert "result_id not found or expired" in mismatched["error"]
