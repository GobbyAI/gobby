"""Tests for WebSocket authentication mixin.

Exercises the real AuthMixin._authenticate method with all code paths:
- Missing Authorization header
- Invalid Authorization format (not Bearer)
- Valid Bearer token with successful callback
- Valid Bearer token with callback returning None (invalid token)
- Valid Bearer token with callback raising exception
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.http11 import Response

from gobby.servers.websocket.auth import AuthMixin
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_wired_callback_rejects_and_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.config.app import DaemonConfig
    from gobby.config.bootstrap import BootstrapConfig
    from gobby.runner_init import servers as runner_servers

    auth_callback = AsyncMock(
        side_effect=lambda token: "local-cli" if token == "daemon-token" else None
    )
    websocket_init: dict[str, object] = {}

    class FakeHTTPServer:
        def __init__(self, *, services: object, **_kwargs: object) -> None:
            self.services = services
            self.auth_service = SimpleNamespace(
                verify_ws_token=auth_callback,
                bind_runtime=lambda **_kwargs: None,
                local_token=lambda: "operator-token",
            )
            self._internal_manager = object()
            self.broadcaster = SimpleNamespace(websocket_server=None)

        def set_runner_getter(self, getter: object) -> None:
            self.runner_getter = getter

    class FakeWebSocketServer:
        def __init__(self, **kwargs: object) -> None:
            websocket_init.update(kwargs)

        def configure_terminals(self, *args: object, **kwargs: object) -> None:
            pass

        async def broadcast_config_event(self, _event: object) -> None:
            pass

    runner = MagicMock()
    runner.config = DaemonConfig(websocket={"enabled": True})
    runner.bootstrap_config = BootstrapConfig()
    runner.codex_client = None
    runner.machine_id = "8f000000-0000-4000-8000-000000000001"
    runner.wake_dispatcher = MagicMock()
    runner.cron_scheduler = None
    runner.system_automation_loop = None
    runner.communications_manager = None
    runner.pipeline_executor = None
    runner.message_processor = None
    runner.agent_lifecycle_monitor = None
    runner.attention_manager = None
    runner.attention_metadata_store = None
    runner._dev_mode = False

    monkeypatch.setattr(runner_servers, "HTTPServer", FakeHTTPServer)
    monkeypatch.setattr(runner_servers, "WebSocketServer", FakeWebSocketServer)
    monkeypatch.setattr(runner_servers, "set_app_context", MagicMock())
    monkeypatch.setattr(runner_servers, "CapabilityRefreshCoordinator", MagicMock())
    monkeypatch.setattr(
        runner_servers.WebChatRuntimeManager, "__init__", lambda self, **kwargs: None
    )
    monkeypatch.setattr(
        "gobby.adapters.codex_impl.app_server_adapter.CodexAdapter.is_codex_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "gobby.runner_broadcasting.setup_agent_event_broadcasting",
        lambda _server: None,
    )

    runner_servers.init_servers(runner)

    wired_callback = websocket_init.get("auth_callback")
    assert wired_callback is auth_callback

    auth_server = WebSocketServer(
        config=WebSocketConfig(),
        mcp_manager=MagicMock(),
        auth_callback=wired_callback,
    )
    websocket = _make_ws()
    missing = await auth_server._authenticate(websocket, _make_request())
    accepted = await auth_server._authenticate(
        websocket,
        _make_request("Bearer daemon-token"),
    )

    assert missing is not None
    assert missing.status_code == 401
    assert accepted is None
    assert websocket.user_id == "local-cli"
    auth_callback.assert_awaited_once_with("daemon-token")


class ConcreteAuthServer(AuthMixin):
    """Concrete class using AuthMixin for testing."""

    def __init__(
        self,
        auth_callback: Callable[[str], Coroutine[Any, Any, str | None]] | None = None,
    ) -> None:
        self.auth_callback = auth_callback


def _make_ws(remote_address: tuple[str, int] = ("127.0.0.1", 9999)) -> MagicMock:
    """Create a mock websocket connection object."""
    ws = MagicMock()
    ws.remote_address = remote_address
    return ws


def _make_request(auth_header: str | None = None) -> MagicMock:
    """Create a mock HTTP request with optional Authorization header."""
    request = MagicMock()
    request.headers = MagicMock()
    request.headers.get = MagicMock(return_value=auth_header)
    return request


class TestMissingAuthHeader:
    """Tests when auth_callback is set but no Authorization header is provided."""

    async def test_rejects_with_401(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header=None)

        result = await server._authenticate(ws, request)

        assert isinstance(result, Response)
        assert result.status_code == 401

    async def test_401_body_mentions_missing_header(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header=None)

        result = await server._authenticate(ws, request)

        assert b"Missing Authorization header" in result.body

    async def test_callback_not_called(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header=None)

        await server._authenticate(ws, request)

        callback.assert_not_called()
        assert callback.call_count == 0
        assert not callback.called


class TestInvalidAuthFormat:
    """Tests when Authorization header doesn't start with 'Bearer '."""

    async def test_basic_auth_rejected(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Basic dXNlcjpwYXNz")

        result = await server._authenticate(ws, request)

        assert isinstance(result, Response)
        assert result.status_code == 401

    async def test_bearer_lowercase_rejected(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="bearer some-token")

        result = await server._authenticate(ws, request)

        assert isinstance(result, Response)
        assert result.status_code == 401

    async def test_raw_token_rejected(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="just-a-raw-token")

        result = await server._authenticate(ws, request)

        assert isinstance(result, Response)
        assert result.status_code == 401

    async def test_401_body_mentions_bearer(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Basic abc")

        result = await server._authenticate(ws, request)

        assert b"Bearer token" in result.body

    async def test_callback_not_called_for_bad_format(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Token abc123")

        await server._authenticate(ws, request)

        callback.assert_not_called()
        assert callback.call_count == 0
        assert not callback.called


class TestValidBearerToken:
    """Tests when Bearer token is valid and callback returns a user_id."""

    async def test_accepts_connection(self) -> None:
        callback = AsyncMock(return_value="user-123")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer valid-token-abc")

        result = await server._authenticate(ws, request)

        assert result is None

    async def test_assigns_user_id_from_callback(self) -> None:
        callback = AsyncMock(return_value="user-42")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer my-token")

        await server._authenticate(ws, request)

        assert ws.user_id == "user-42"

    async def test_callback_receives_token_without_bearer_prefix(self) -> None:
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer the-actual-token")

        await server._authenticate(ws, request)

        callback.assert_called_once_with("the-actual-token")
        assert callback.call_count == 1
        assert callback.call_args is not None

    async def test_empty_string_token_still_passed(self) -> None:
        """'Bearer ' with empty token should still call callback with ''."""
        callback = AsyncMock(return_value="user-1")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer ")

        await server._authenticate(ws, request)

        callback.assert_called_once_with("")
        assert callback.call_count == 1
        assert callback.call_args is not None


class TestInvalidToken:
    """Tests when callback returns None (invalid/expired token)."""

    async def test_rejects_with_403(self) -> None:
        callback = AsyncMock(return_value=None)
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer expired-token")

        result = await server._authenticate(ws, request)

        assert isinstance(result, Response)
        assert result.status_code == 403

    async def test_403_body_mentions_invalid_token(self) -> None:
        callback = AsyncMock(return_value=None)
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer expired-token")

        result = await server._authenticate(ws, request)

        assert b"Invalid token" in result.body

    async def test_empty_string_user_id_treated_as_invalid(self) -> None:
        """Callback returning empty string should be treated as invalid."""
        callback = AsyncMock(return_value="")
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer some-token")

        result = await server._authenticate(ws, request)

        assert isinstance(result, Response)
        assert result.status_code == 403


class TestAuthCallbackException:
    """Tests when the auth callback raises an exception."""

    async def test_rejects_with_500(self) -> None:
        callback = AsyncMock(side_effect=RuntimeError("auth service down"))
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer some-token")

        result = await server._authenticate(ws, request)

        assert isinstance(result, Response)
        assert result.status_code == 500

    async def test_500_body_mentions_internal_error(self) -> None:
        callback = AsyncMock(side_effect=ConnectionError("timeout"))
        server = ConcreteAuthServer(auth_callback=callback)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer some-token")

        result = await server._authenticate(ws, request)

        assert b"Internal server error" in result.body

    async def test_different_exception_types_all_return_500(self) -> None:
        """All exception types should be caught and return 500."""
        exceptions = [
            ValueError("bad value"),
            TypeError("bad type"),
            KeyError("missing key"),
            OSError("network error"),
        ]
        for exc in exceptions:
            callback = AsyncMock(side_effect=exc)
            server = ConcreteAuthServer(auth_callback=callback)
            ws = _make_ws()
            request = _make_request(auth_header="Bearer token")

            result = await server._authenticate(ws, request)

            assert isinstance(result, Response)
            assert result.status_code == 500, f"Expected 500 for {type(exc).__name__}"
