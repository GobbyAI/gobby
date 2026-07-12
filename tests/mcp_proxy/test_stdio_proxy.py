"""Focused bearer-auth tests for stdio daemon requests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.session_bootstrap import resolve_session_id_from_terminal_context
from gobby.mcp_proxy.stdio_proxy import DaemonProxy

pytestmark = pytest.mark.unit


def _response(status_code: int, payload: dict[str, object] | None = None) -> MagicMock:
    response = MagicMock(status_code=status_code, text="Unauthorized")
    response.json.return_value = payload or {"success": True}
    return response


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
            client_cls.return_value.__aenter__.return_value = client
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
        client_cls.return_value.__aenter__.return_value = client
        result = await proxy._request(
            "POST",
            "/api/mcp/example/tools/nullable_result",
            json={},
        )

    assert result == payload


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
