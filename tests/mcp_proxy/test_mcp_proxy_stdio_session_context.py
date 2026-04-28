from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.stdio import DaemonProxy

pytestmark = pytest.mark.unit


def _mock_response(payload: dict[str, Any] | None = None) -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = payload or {"success": True}
    return response


def _mock_http_client(
    mock_client_cls: MagicMock, payload: dict[str, Any] | None = None
) -> AsyncMock:
    client = AsyncMock()
    client.request = AsyncMock(return_value=_mock_response(payload))
    mock_client_cls.return_value.__aenter__.return_value = client
    return client


@pytest.mark.asyncio
async def test_request_session_id_override_replaces_stale_cached_header() -> None:
    proxy = DaemonProxy(60887)
    proxy._session_id = "old-session"

    with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
        client = _mock_http_client(mock_client_cls)

        await proxy._request(
            "POST",
            "/some/path",
            json={"session_id": "target-session"},
            session_id="new-session",
        )

    _, kwargs = client.request.call_args
    assert kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert proxy._session_id == "new-session"


@pytest.mark.asyncio
async def test_request_body_session_id_does_not_update_context_header() -> None:
    proxy = DaemonProxy(60887)

    with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
        client = _mock_http_client(mock_client_cls)

        await proxy._request("POST", "/some/path", json={"session_id": "target-session"})

    _, kwargs = client.request.call_args
    assert "X-Gobby-Session-Id" not in kwargs["headers"]
    assert proxy._session_id is None


@pytest.mark.asyncio
async def test_call_tool_session_id_override_replaces_stale_cached_header() -> None:
    proxy = DaemonProxy(60887)
    proxy._session_id = "old-session"

    with patch("gobby.mcp_proxy.stdio.load_config") as mock_config:
        mock_config.return_value = MagicMock(mcp_client_proxy=MagicMock(tool_timeouts={}))
        with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
            client = _mock_http_client(mock_client_cls)

            await proxy.call_tool(
                "gobby-tasks",
                "list_tasks",
                {"status": "open"},
                session_id="new-session",
            )

    _, kwargs = client.request.call_args
    assert kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert kwargs["json"] == {"status": "open"}
    assert proxy._session_id == "new-session"


@pytest.mark.asyncio
async def test_variable_tools_send_requested_session_as_target_and_header() -> None:
    proxy = DaemonProxy(60887)
    proxy._session_id = "old-session"

    with patch("gobby.mcp_proxy.stdio.httpx.AsyncClient") as mock_client_cls:
        client = _mock_http_client(mock_client_cls)

        await proxy.set_variable("flag", True, session_id="new-session")
        await proxy.get_variable("flag", session_id="new-session")

    set_call, get_call = client.request.call_args_list
    assert set_call.kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert set_call.kwargs["json"] == {
        "name": "flag",
        "value": True,
        "session_id": "new-session",
    }
    assert get_call.kwargs["headers"]["X-Gobby-Session-Id"] == "new-session"
    assert get_call.kwargs["json"] == {"name": "flag", "session_id": "new-session"}
