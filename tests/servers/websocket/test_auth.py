"""Tests for WebSocket authentication mixin.

Exercises the real AuthMixin._authenticate method with all code paths:
- Local-first mode (no auth_callback)
- Missing Authorization header
- Invalid Authorization format (not Bearer)
- Valid Bearer token with successful callback
- Valid Bearer token with callback returning None (invalid token)
- Valid Bearer token with callback raising exception
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.http11 import Response

from gobby.servers.websocket.auth import AuthMixin

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class ConcreteAuthServer(AuthMixin):
    """Concrete class using AuthMixin for testing."""

    def __init__(self, auth_callback=None):
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


class TestLocalFirstMode:
    """Tests for local-first mode (auth_callback=None)."""

    async def test_accepts_connection(self) -> None:
        server = ConcreteAuthServer(auth_callback=None)
        ws = _make_ws()
        request = _make_request()

        result = await server._authenticate(ws, request)

        assert result is None

    async def test_assigns_local_user_id(self) -> None:
        server = ConcreteAuthServer(auth_callback=None)
        ws = _make_ws()
        request = _make_request()

        await server._authenticate(ws, request)

        assert hasattr(ws, "user_id")
        assert ws.user_id.startswith("local-")

    async def test_local_user_id_has_hex_suffix(self) -> None:
        server = ConcreteAuthServer(auth_callback=None)
        ws = _make_ws()
        request = _make_request()

        await server._authenticate(ws, request)

        # Format is "local-" + 8 hex chars
        prefix, hex_part = ws.user_id.split("-", 1)
        assert prefix == "local"
        assert len(hex_part) == 8
        # Validate it's valid hex
        int(hex_part, 16)

    async def test_each_connection_gets_unique_id(self) -> None:
        server = ConcreteAuthServer(auth_callback=None)
        ids = set()
        for _ in range(10):
            ws = _make_ws()
            await server._authenticate(ws, _make_request())
            ids.add(ws.user_id)
        assert len(ids) == 10

    async def test_ignores_auth_header_in_local_mode(self) -> None:
        """Even if auth header is present, local mode ignores it."""
        server = ConcreteAuthServer(auth_callback=None)
        ws = _make_ws()
        request = _make_request(auth_header="Bearer some-token")

        result = await server._authenticate(ws, request)

        assert result is None
        assert ws.user_id.startswith("local-")


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

        assert "Missing Authorization header" in result.reason_phrase

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

        assert "Bearer token" in result.reason_phrase

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

        assert "Invalid token" in result.reason_phrase

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

        assert "Internal server error" in result.reason_phrase

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
