"""Focused bearer-auth tests for stdio daemon requests."""

import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.server.fastmcp import FastMCP

from gobby.mcp_proxy.session_bootstrap import resolve_session_id_from_terminal_context
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.mcp_proxy.stdio_server import StdioServerDependencies, create_stdio_mcp_server
from gobby.mcp_proxy.stdio_tools import register_proxy_tools
from tests.mcp_proxy.result_offload_test_support import TEST_MAX_ENVELOPE_CHARS
from tests.mcp_proxy.tool_capture import async_tool_capture_mock

pytestmark = pytest.mark.unit


def _response(status_code: int, payload: dict[str, object] | None = None) -> MagicMock:
    response = MagicMock(status_code=status_code, text="Unauthorized")
    response.json.return_value = payload or {"success": True}
    return response


def _capture_stdio_tools(
    proxy: MagicMock,
) -> dict[str, Callable[..., Awaitable[Any]]]:
    mcp, captured = async_tool_capture_mock()
    register_proxy_tools(mcp, proxy)
    return captured


@pytest.mark.asyncio
async def test_request_auth_retry() -> None:
    old_headers = {"Authorization": "Bearer old-token"}
    fresh_headers = {"Authorization": "Bearer fresh-token"}

    with patch(
        "gobby.mcp_proxy.stdio_proxy.daemon_auth_headers",
        side_effect=[old_headers, fresh_headers],
    ):
        proxy = DaemonProxy(60887)
        client = AsyncMock()
        client.request = AsyncMock(side_effect=[_response(401), _response(200, {"success": True})])

        with patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient") as client_cls:
            client_cls.return_value = client
            result = await proxy._request("POST", "/api/workflows/variables/set", json={})

    assert result == {"success": True}
    assert client.request.await_count == 2
    assert client.request.await_args_list[0].kwargs["headers"]["Authorization"] == (
        "Bearer old-token"
    )
    assert client.request.await_args_list[1].kwargs["headers"]["Authorization"] == (
        "Bearer fresh-token"
    )


@pytest.mark.asyncio
async def test_request_preserves_explicit_nulls_in_tool_results() -> None:
    payload = {
        "success": True,
        "result": {
            "content": [{"type": "text", "text": None}],
            "structuredContent": {"value": None, "nested": [None, {"item": None}]},
        },
    }
    client = AsyncMock()
    client.request = AsyncMock(return_value=_response(200, payload))
    proxy = DaemonProxy(60887)

    with patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient") as client_cls:
        client_cls.return_value = client
        result = await proxy._request(
            "POST",
            "/api/mcp/example/tools/nullable_result",
            json={},
        )

    assert result == payload


@pytest.mark.asyncio
async def test_call_tool_sends_intent_on_each_http_shape() -> None:
    deps = MagicMock()
    deps.read_project_id.return_value = None
    deps.load_config.return_value = MagicMock(mcp_client_proxy=MagicMock(tool_timeouts={}))
    proxy = DaemonProxy(60887, deps_factory=lambda: deps)
    long_intent = "wrapper-summary" * 200

    with patch.object(proxy, "_request", new_callable=AsyncMock) as request:
        request.return_value = {"success": True}
        await proxy.call_tool(
            "example",
            "ordinary",
            {"intent": "target-value"},
            intent=long_intent,
        )
        await proxy.call_tool(
            "gobby-sessions",
            "wait_for_summary",
            {"session_id": "session", "intent": "target-value"},
            intent=long_intent,
        )

    direct = request.await_args_list[0]
    assert direct.args[:2] == ("POST", "/api/mcp/example/tools/ordinary")
    assert direct.kwargs["json"] == {"intent": "target-value"}
    assert direct.kwargs["params"] == {"intent": long_intent[:1_024]}
    structured = request.await_args_list[1]
    assert structured.args[:2] == ("POST", "/api/mcp/tools/call")
    assert structured.kwargs["json"]["intent"] == long_intent
    assert structured.kwargs["json"]["arguments"]["intent"] == "target-value"


@pytest.mark.asyncio
async def test_stdio_final_wait_envelope_stays_within_shared_cap() -> None:
    http_envelope = {
        "success": True,
        "result": {
            "offloaded": True,
            "result_id": "11111111-1111-4111-8111-111111111111",
            "preview": "x" * 1_200,
        },
        "response_time_ms": 1.0,
    }
    proxy = MagicMock(spec=DaemonProxy)
    proxy.call_tool = AsyncMock(return_value=http_envelope)
    call_tool = _capture_stdio_tools(proxy)["call_tool"]

    result = await call_tool(
        server_name="gobby-sessions",
        tool_name="wait_for_summary",
        arguments={"timeout_seconds": 999_999},
        intent="find completion",
    )

    assert len(json.dumps(result, ensure_ascii=False, default=str)) <= TEST_MAX_ENVELOPE_CHARS
    assert result["wait_timeout_capped_by_mcp_wrapper"] is True
    assert proxy.call_tool.await_args.kwargs["intent"] == "find completion"


@pytest.mark.asyncio
async def test_stdio_final_retrieval_response_stays_within_shared_cap() -> None:
    http_result: dict[str, Any] = {
        "success": True,
        "result": {
            "result_id": "11111111-1111-4111-8111-111111111111",
            "content": "x" * 1_400,
            "offset": 0,
            "next_offset": 1_400,
            "total_chars": 4_000,
        },
        "response_time_ms": 1.0,
    }
    proxy = MagicMock(spec=DaemonProxy)
    proxy.call_tool = AsyncMock(return_value=http_result)
    call_tool = _capture_stdio_tools(proxy)["call_tool"]

    result = await call_tool(
        server_name="gobby-results",
        tool_name="get_tool_result",
        arguments={"result_id": http_result["result"]["result_id"]},
    )

    assert result == http_result
    assert len(json.dumps(result, ensure_ascii=False, default=str)) <= TEST_MAX_ENVELOPE_CHARS


@pytest.mark.asyncio
async def test_requests_reuse_client_and_close_it_once() -> None:
    client = AsyncMock()
    client.request = AsyncMock(return_value=_response(200))
    proxy = DaemonProxy(60887)
    proxy._project_id = None

    with patch("gobby.mcp_proxy.stdio_proxy.httpx.AsyncClient", return_value=client) as client_cls:
        first = await proxy._request("GET", "/api/admin/status")
        second = await proxy._request("GET", "/api/admin/status")
        await proxy.aclose()
        await proxy.aclose()

    assert first == {"success": True}
    assert second == {"success": True}
    client_cls.assert_called_once_with()
    assert client.request.await_count == 2
    client.aclose.assert_awaited_once_with()


def _create_server_with_proxy(proxy: DaemonProxy) -> FastMCP:
    dependencies = StdioServerDependencies(
        load_config=lambda: MagicMock(daemon_port=60887),
        setup_internal_registries=MagicMock(),
        build_gobby_instructions=lambda: "instructions",
        fast_mcp_factory=FastMCP,
        proxy_factory=MagicMock(return_value=proxy),
        register_proxy_tools=MagicMock(),
    )
    return create_stdio_mcp_server(deps=dependencies)


@pytest.mark.asyncio
async def test_stdio_lifespan_closes_proxy_after_normal_completion() -> None:
    proxy = MagicMock(spec=DaemonProxy)
    proxy.aclose = AsyncMock()
    server = _create_server_with_proxy(proxy)
    lifespan = server.settings.lifespan

    assert lifespan is not None
    async with lifespan(server):
        pass

    proxy.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stdio_lifespan_closes_proxy_when_server_run_raises() -> None:
    proxy = MagicMock(spec=DaemonProxy)
    proxy.aclose = AsyncMock()
    server = _create_server_with_proxy(proxy)
    lifespan = server.settings.lifespan

    assert lifespan is not None
    with pytest.raises(RuntimeError, match="server stopped"):
        async with lifespan(server):
            raise RuntimeError("server stopped")

    proxy.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_get_tool_schema_strips_explicit_nulls() -> None:
    proxy = DaemonProxy(60887)
    schema_response = {
        "name": "nullable_schema",
        "description": None,
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {
                    "type": ["string", "null"],
                    "default": None,
                }
            },
        },
    }

    with patch.object(proxy, "_request", new_callable=AsyncMock) as request:
        request.return_value = schema_response
        result = await proxy.get_tool_schema("example", "nullable_schema")

    assert result == {
        "success": True,
        "tool": {
            "name": "nullable_schema",
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": ["string", "null"]}},
            },
        },
    }


@pytest.mark.asyncio
async def test_list_tools_strips_explicit_nulls_from_schemas() -> None:
    proxy = DaemonProxy(60887)
    listing = {
        "success": True,
        "tools": [
            {
                "name": "nullable_schema",
                "description": None,
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"default": None}},
                },
            }
        ],
    }

    with patch.object(proxy, "_request", new_callable=AsyncMock) as request:
        request.return_value = listing
        result = await proxy.list_tools("example")

    assert result == {
        "success": True,
        "tools": [
            {
                "name": "nullable_schema",
                "inputSchema": {"type": "object", "properties": {"value": {}}},
            }
        ],
    }


@pytest.mark.asyncio
async def test_session_bootstrap_sends_auth_header() -> None:
    response = _response(200, {"session": {"id": "session-123"}})
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)

    with (
        patch(
            "gobby.mcp_proxy.session_bootstrap.daemon_auth_headers",
            return_value={"Authorization": "Bearer bootstrap-token"},
        ),
        patch("gobby.mcp_proxy.session_bootstrap.httpx.AsyncClient") as client_cls,
    ):
        client_cls.return_value.__aenter__.return_value = client
        session_id = await resolve_session_id_from_terminal_context(
            "http://127.0.0.1:60887", "project-123"
        )

    assert session_id == "session-123"
    assert client.post.await_args.kwargs["headers"] == {"Authorization": "Bearer bootstrap-token"}
