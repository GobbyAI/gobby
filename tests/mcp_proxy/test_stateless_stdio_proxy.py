"""Stateless caller-identity behavior for the stdio daemon proxy."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.utils.session_context import TERMINAL_CONTEXT_HEADER

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TERMINAL_CONTEXT = {"parent_pid": 4321, "tmux_pane": "%4"}
HEADLESS_CONTEXT = {"parent_pid": 4321}


def _proxy_with_response(response: MagicMock) -> tuple[DaemonProxy, AsyncMock]:
    client = MagicMock()
    client.request = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    deps = MagicMock()
    deps.read_project_id.return_value = "caller-project"
    deps.http_client_factory.return_value = client
    proxy = DaemonProxy(60887, deps_factory=lambda: deps)
    return proxy, client.request


def _success_response() -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = {"success": True}
    return response


@pytest.mark.parametrize(
    ("environment_session", "explicit_session", "context", "expected_session", "ambient"),
    [
        (None, None, TERMINAL_CONTEXT, None, True),
        ("env-session", None, TERMINAL_CONTEXT, "env-session", False),
        (None, "explicit-session", TERMINAL_CONTEXT, None, True),
        (None, "explicit-session", HEADLESS_CONTEXT, "explicit-session", False),
        ("env-session", "explicit-session", TERMINAL_CONTEXT, "env-session", False),
        ("env-session", "explicit-session", HEADLESS_CONTEXT, "env-session", False),
    ],
)
async def test_request_identity_header_matrix(
    monkeypatch: pytest.MonkeyPatch,
    environment_session: str | None,
    explicit_session: str | None,
    context: dict[str, object],
    expected_session: str | None,
    ambient: bool,
) -> None:
    if environment_session is None:
        monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("GOBBY_SESSION_ID", environment_session)

    with patch(
        "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
        return_value=context,
    ):
        proxy, request = _proxy_with_response(_success_response())

    await proxy._request("GET", "/api/status", session_id=explicit_session)

    assert request.await_args is not None
    headers = request.await_args.kwargs["headers"]
    assert headers.get("X-Gobby-Session-Id") == expected_session
    if ambient:
        assert json.loads(headers[TERMINAL_CONTEXT_HEADER]) == context
    else:
        assert TERMINAL_CONTEXT_HEADER not in headers


async def test_explicit_request_does_not_change_later_ambient_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
    with patch(
        "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
        return_value=TERMINAL_CONTEXT,
    ):
        proxy, request = _proxy_with_response(_success_response())

    await proxy._request("GET", "/api/status", session_id="explicit-session")
    await proxy._request("GET", "/api/status")

    explicit_headers = request.await_args_list[0].kwargs["headers"]
    ambient_headers = request.await_args_list[1].kwargs["headers"]
    assert "X-Gobby-Session-Id" not in explicit_headers
    assert json.loads(explicit_headers[TERMINAL_CONTEXT_HEADER]) == TERMINAL_CONTEXT
    assert "X-Gobby-Session-Id" not in ambient_headers
    assert json.loads(ambient_headers[TERMINAL_CONTEXT_HEADER]) == TERMINAL_CONTEXT


async def test_grok_session_id_is_not_caller_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
    monkeypatch.setenv("GROK_SESSION_ID", "grok-external-id")
    with patch(
        "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
        return_value=TERMINAL_CONTEXT,
    ):
        proxy, request = _proxy_with_response(_success_response())

    await proxy._request("GET", "/api/status", session_id="#1")

    assert request.await_args is not None
    headers = request.await_args.kwargs["headers"]
    assert "X-Gobby-Session-Id" not in headers
    assert json.loads(headers[TERMINAL_CONTEXT_HEADER]) == TERMINAL_CONTEXT


async def test_terminal_context_is_captured_once_at_proxy_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_SESSION_ID", raising=False)
    with patch(
        "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
        return_value=TERMINAL_CONTEXT,
    ) as capture:
        proxy, request = _proxy_with_response(_success_response())
        await proxy._request("GET", "/api/status")
        await proxy._request("GET", "/api/status")

    capture.assert_called_once_with()
    first_headers = request.await_args_list[0].kwargs["headers"]
    second_headers = request.await_args_list[1].kwargs["headers"]
    assert first_headers[TERMINAL_CONTEXT_HEADER] == second_headers[TERMINAL_CONTEXT_HEADER]


async def test_session_required_409_returns_typed_daemon_detail() -> None:
    detail: dict[str, Any] = {
        "success": False,
        "error_code": "SESSION_REQUIRED",
        "error": "Wrapper caller session could not be resolved",
        "terminal_context_seen": True,
    }
    response = MagicMock(status_code=409, text='{"detail":{...}}')
    response.json.return_value = {"detail": detail}
    with patch(
        "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
        return_value=TERMINAL_CONTEXT,
    ):
        proxy, _request = _proxy_with_response(response)

    assert await proxy._request("GET", "/api/status") == detail


async def test_other_http_errors_keep_generic_transport_result() -> None:
    response = MagicMock(status_code=500, text="daemon exploded")
    with patch(
        "gobby.mcp_proxy.stdio_proxy.current_terminal_context",
        return_value=TERMINAL_CONTEXT,
    ):
        proxy, _request = _proxy_with_response(response)

    assert await proxy._request("GET", "/api/status") == {
        "success": False,
        "error": "HTTP 500: daemon exploded",
    }
