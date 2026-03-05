"""Tests for stdio transport connection.

Exercises the real StdioTransportConnection code paths. Only the MCP SDK's
stdio_client and ClientSession are mocked (external I/O). The env-var
expansion helpers are tested against real os.environ.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.stdio import (
    StdioTransportConnection,
    _expand_args,
    _expand_env_dict,
    _expand_env_var,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MCPServerConfig:
    """Create a real MCPServerConfig for stdio transport."""
    defaults = {
        "name": "test-stdio",
        "project_id": "proj-002",
        "transport": "stdio",
        "command": "node",
        "args": ["server.js", "--port", "3000"],
        "env": None,
    }
    defaults.update(overrides)
    return MCPServerConfig(**defaults)


def _mock_session() -> AsyncMock:
    """Create a mock ClientSession with initialize()."""
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=[])
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> MCPServerConfig:
    return _make_config()


@pytest.fixture
def conn(config: MCPServerConfig) -> StdioTransportConnection:
    return StdioTransportConnection(config)


# ===========================================================================
# Environment variable expansion — _expand_env_var
# ===========================================================================


class TestExpandEnvVar:
    def test_plain_text_unchanged(self) -> None:
        assert _expand_env_var("plain text") == "plain text"

    def test_empty_string(self) -> None:
        assert _expand_env_var("") == ""

    def test_existing_var_expanded(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert _expand_env_var("${MY_VAR}") == "hello"

    def test_missing_var_no_default_unchanged(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "NONEXISTENT_XYZ"}
        with patch.dict(os.environ, env_clean, clear=True):
            assert _expand_env_var("${NONEXISTENT_XYZ}") == "${NONEXISTENT_XYZ}"

    def test_missing_var_with_default(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "MISSING_ABC"}
        with patch.dict(os.environ, env_clean, clear=True):
            assert _expand_env_var("${MISSING_ABC:-fallback}") == "fallback"

    def test_existing_var_ignores_default(self) -> None:
        with patch.dict(os.environ, {"PRESENT": "real"}):
            assert _expand_env_var("${PRESENT:-fallback}") == "real"

    def test_empty_var_uses_default(self) -> None:
        with patch.dict(os.environ, {"EMPTY_V": ""}):
            assert _expand_env_var("${EMPTY_V:-fallback}") == "fallback"

    def test_empty_default_string(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "NOPE"}
        with patch.dict(os.environ, env_clean, clear=True):
            assert _expand_env_var("${NOPE:-}") == ""

    def test_multiple_vars_in_one_string(self) -> None:
        with patch.dict(os.environ, {"HOST": "localhost", "PORT": "8080"}):
            assert _expand_env_var("${HOST}:${PORT}") == "localhost:8080"

    def test_mixed_vars_and_plain_text(self) -> None:
        with patch.dict(os.environ, {"DB": "mydb"}):
            assert _expand_env_var("postgres://${DB}/data") == "postgres://mydb/data"

    def test_var_with_underscores_and_digits(self) -> None:
        with patch.dict(os.environ, {"MY_VAR_2": "works"}):
            assert _expand_env_var("${MY_VAR_2}") == "works"


# ===========================================================================
# _expand_env_dict
# ===========================================================================


class TestExpandEnvDict:
    def test_none_returns_none(self) -> None:
        assert _expand_env_dict(None) is None

    def test_empty_dict(self) -> None:
        assert _expand_env_dict({}) == {}

    def test_expands_values(self) -> None:
        with patch.dict(os.environ, {"SECRET": "s3cr3t"}):
            result = _expand_env_dict({"API_KEY": "${SECRET}"})
            assert result == {"API_KEY": "s3cr3t"}

    def test_keys_not_expanded(self) -> None:
        with patch.dict(os.environ, {"K": "v"}):
            result = _expand_env_dict({"${K}": "literal"})
            # Key is still "${K}" — only values are expanded
            assert result == {"${K}": "literal"}

    def test_multiple_entries(self) -> None:
        with patch.dict(os.environ, {"A": "1", "B": "2"}):
            result = _expand_env_dict({"x": "${A}", "y": "${B}", "z": "plain"})
            assert result == {"x": "1", "y": "2", "z": "plain"}


# ===========================================================================
# _expand_args
# ===========================================================================


class TestExpandArgs:
    def test_none_returns_none(self) -> None:
        assert _expand_args(None) is None

    def test_empty_list(self) -> None:
        assert _expand_args([]) == []

    def test_expands_args(self) -> None:
        with patch.dict(os.environ, {"PORT": "9090"}):
            result = _expand_args(["--port", "${PORT}"])
            assert result == ["--port", "9090"]

    def test_mixed_plain_and_vars(self) -> None:
        with patch.dict(os.environ, {"DIR": "/tmp"}):
            result = _expand_args(["--dir", "${DIR}", "--verbose"])
            assert result == ["--dir", "/tmp", "--verbose"]


# ===========================================================================
# StdioTransportConnection — init
# ===========================================================================


class TestStdioInit:
    def test_initial_state(self, conn: StdioTransportConnection) -> None:
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn.session is None
        assert conn._session_context is None
        assert conn._transport_context is None

    def test_config_stored(self, conn: StdioTransportConnection) -> None:
        assert conn.config.name == "test-stdio"
        assert conn.config.command == "node"
        assert conn.config.args == ["server.js", "--port", "3000"]

    def test_auth_token_and_callback(self, config: MCPServerConfig) -> None:
        async def refresh() -> str:
            return "new-token"

        c = StdioTransportConnection(config, auth_token="tok", token_refresh_callback=refresh)
        assert c._auth_token == "tok"
        assert c._token_refresh_callback is refresh


# ===========================================================================
# connect() — already connected
# ===========================================================================


class TestStdioConnectAlreadyConnected:
    @pytest.mark.asyncio
    async def test_returns_existing_session(self, conn: StdioTransportConnection) -> None:
        fake_session = MagicMock()
        conn._state = ConnectionState.CONNECTED
        conn._session = fake_session

        result = await conn.connect()
        assert result is fake_session
        assert conn.state == ConnectionState.CONNECTED


# ===========================================================================
# connect() — successful
# ===========================================================================


class TestStdioConnectSuccess:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_full_connect(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        mock_read = MagicMock()
        mock_write = MagicMock()

        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(mock_read, mock_write))
        mock_stdio_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_session_cls.return_value = mock_session_ctx

        result = await conn.connect()

        assert result is mock_session
        assert conn.state == ConnectionState.CONNECTED
        assert conn.is_connected is True
        assert conn._consecutive_failures == 0
        assert conn._session is mock_session
        assert conn._session_context is mock_session_ctx
        assert conn._transport_context is mock_transport_ctx

    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_connect_creates_stdio_server_parameters(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
    ) -> None:
        """Verify StdioServerParameters is created with expanded args and env."""
        with patch.dict(os.environ, {"MY_PORT": "5555"}):
            cfg = _make_config(args=["--port", "${MY_PORT}"], env={"KEY": "${MY_PORT}"})
            c = StdioTransportConnection(cfg)

            mock_transport_ctx = AsyncMock()
            mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
            mock_stdio_client.return_value = mock_transport_ctx

            mock_session = _mock_session()
            mock_session_ctx = AsyncMock()
            mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
            mock_client_session_cls.return_value = mock_session_ctx

            await c.connect()

            # Verify StdioServerParameters was passed to stdio_client
            call_args = mock_stdio_client.call_args
            params = call_args[0][0]
            assert params.command == "node"
            assert params.args == ["--port", "5555"]
            assert params.env == {"KEY": "5555"}

    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_connect_with_none_args_uses_empty_list(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
    ) -> None:
        """When args is None, expanded_args defaults to []."""
        cfg = _make_config(args=None)
        c = StdioTransportConnection(cfg)

        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_stdio_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_client_session_cls.return_value = mock_session_ctx

        await c.connect()

        params = mock_stdio_client.call_args[0][0]
        assert params.args == []
        assert params.env is None


# ===========================================================================
# connect() — missing command
# ===========================================================================


class TestStdioConnectMissingCommand:
    @pytest.mark.asyncio
    async def test_missing_command_raises_mcp_error(self) -> None:
        cfg = _make_config()
        cfg.command = None
        c = StdioTransportConnection(cfg)

        with pytest.raises(MCPError, match="Stdio connection failed"):
            await c.connect()

        assert c.state == ConnectionState.FAILED
        assert c._session is None
        assert c._session_context is None
        assert c._transport_context is None


# ===========================================================================
# connect() — transport entry failure (before session)
# ===========================================================================


class TestStdioConnectTransportFailure:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_transport_aenter_failure(
        self,
        mock_stdio_client: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        """If transport __aenter__ fails, no cleanup of session/transport needed."""
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(side_effect=OSError("spawn failed"))
        mock_stdio_client.return_value = mock_transport_ctx

        with pytest.raises(MCPError, match="Stdio connection failed.*spawn failed"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._session is None
        assert conn._transport_context is None


# ===========================================================================
# connect() — session entry failure (transport entered, session fails)
# ===========================================================================


class TestStdioConnectSessionFailure:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_session_aenter_failure_cleans_transport(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        """If session __aenter__ fails, transport context is cleaned up."""
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_stdio_client.return_value = mock_transport_ctx

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("session init boom"))
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="Stdio connection failed.*session init boom"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        # Transport __aexit__ was called for cleanup
        mock_transport_ctx.__aexit__.assert_awaited_once()


# ===========================================================================
# connect() — initialize() failure (both entered, init fails)
# ===========================================================================


class TestStdioConnectInitializeFailure:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_initialize_failure_cleans_both(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        """If session.initialize() fails, both session and transport are cleaned."""
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_stdio_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session.initialize = AsyncMock(side_effect=ConnectionError("init failed"))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="Stdio connection failed.*init failed"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        mock_session_ctx.__aexit__.assert_awaited_once()
        mock_transport_ctx.__aexit__.assert_awaited_once()


# ===========================================================================
# connect() — cleanup errors during failure are suppressed
# ===========================================================================


class TestStdioConnectCleanupErrors:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_session_cleanup_error_suppressed(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        """If session cleanup raises during error handling, it's logged and suppressed."""
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_stdio_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session.initialize = AsyncMock(side_effect=ValueError("init fail"))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("cleanup boom"))
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="init fail"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED

    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_transport_cleanup_error_suppressed(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        """If transport cleanup raises during error handling, it's logged and suppressed."""
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(side_effect=OSError("transport cleanup fail"))
        mock_stdio_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session.initialize = AsyncMock(side_effect=ValueError("init fail"))

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_ctx

        with pytest.raises(MCPError, match="init fail"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED


# ===========================================================================
# connect() — MCPError not double-wrapped
# ===========================================================================


class TestStdioConnectMCPErrorPassthrough:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_mcp_error_re_raised_directly(
        self,
        mock_stdio_client: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        """An MCPError from transport is re-raised without wrapping."""
        original = MCPError("original mcp error")
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(side_effect=original)
        mock_stdio_client.return_value = mock_transport_ctx

        with pytest.raises(MCPError, match="original mcp error") as exc_info:
            await conn.connect()

        assert exc_info.value is original


# ===========================================================================
# connect() — empty error message
# ===========================================================================


class TestStdioConnectEmptyErrorMessage:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_empty_str_exception_uses_type_name(
        self,
        mock_stdio_client: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        """Exceptions with empty str() get a type-name-based message."""

        class SilentError(Exception):
            def __str__(self) -> str:
                return ""

        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(side_effect=SilentError())
        mock_stdio_client.return_value = mock_transport_ctx

        with pytest.raises(MCPError, match="SilentError.*Connection closed or timed out"):
            await conn.connect()


# ===========================================================================
# disconnect() — no contexts
# ===========================================================================


class TestStdioDisconnectNoContexts:
    @pytest.mark.asyncio
    async def test_disconnect_clean_state(self, conn: StdioTransportConnection) -> None:
        await conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._session_context is None
        assert conn._transport_context is None
        assert conn._session is None


# ===========================================================================
# disconnect() — happy path with both contexts
# ===========================================================================


class TestStdioDisconnectBothContexts:
    @pytest.mark.asyncio
    async def test_cleans_both_contexts(self, conn: StdioTransportConnection) -> None:
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()

        assert conn._session_context is None
        assert conn._session is None
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED

        mock_session_ctx.__aexit__.assert_awaited_once()
        mock_transport_ctx.__aexit__.assert_awaited_once()


# ===========================================================================
# disconnect() — session timeout
# ===========================================================================


class TestStdioDisconnectSessionTimeout:
    @pytest.mark.asyncio
    async def test_session_timeout_handled(self, conn: StdioTransportConnection) -> None:
        """TimeoutError during session close is caught gracefully."""

        async def slow_exit(*args: Any) -> None:
            raise TimeoutError()

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aexit__ = slow_exit
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — session RuntimeError (cancel scope)
# ===========================================================================


class TestStdioDisconnectSessionRuntimeError:
    @pytest.mark.asyncio
    async def test_cancel_scope_error_suppressed(self, conn: StdioTransportConnection) -> None:
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("cannot exit cancel scope"))
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_other_runtime_error_handled(self, conn: StdioTransportConnection) -> None:
        """Non-cancel-scope RuntimeError is still caught."""
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("something else entirely"))
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — session generic Exception
# ===========================================================================


class TestStdioDisconnectSessionGenericError:
    @pytest.mark.asyncio
    async def test_generic_exception_handled(self, conn: StdioTransportConnection) -> None:
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aexit__ = AsyncMock(side_effect=ValueError("weird"))
        conn._session_context = mock_session_ctx
        conn._session = MagicMock()

        await conn.disconnect()
        assert conn._session_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — transport timeout
# ===========================================================================


class TestStdioDisconnectTransportTimeout:
    @pytest.mark.asyncio
    async def test_transport_timeout_handled(self, conn: StdioTransportConnection) -> None:
        async def slow_exit(*args: Any) -> None:
            raise TimeoutError()

        mock_transport_ctx = MagicMock()
        mock_transport_ctx.__aexit__ = slow_exit
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — transport RuntimeError (cancel scope)
# ===========================================================================


class TestStdioDisconnectTransportRuntimeError:
    @pytest.mark.asyncio
    async def test_cancel_scope_error_suppressed(self, conn: StdioTransportConnection) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(side_effect=RuntimeError("cancel scope blah"))
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_other_runtime_error_logged(self, conn: StdioTransportConnection) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(
            side_effect=RuntimeError("unexpected transport error")
        )
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# disconnect() — transport generic Exception
# ===========================================================================


class TestStdioDisconnectTransportGenericError:
    @pytest.mark.asyncio
    async def test_generic_exception_handled(self, conn: StdioTransportConnection) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aexit__ = AsyncMock(side_effect=OSError("broken pipe"))
        conn._transport_context = mock_transport_ctx

        await conn.disconnect()
        assert conn._transport_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# Full connect -> disconnect cycle
# ===========================================================================


class TestStdioFullLifecycle:
    @pytest.mark.asyncio
    @patch("gobby.mcp_proxy.transports.stdio.ClientSession")
    @patch("gobby.mcp_proxy.transports.stdio.stdio_client")
    async def test_connect_then_disconnect(
        self,
        mock_stdio_client: MagicMock,
        mock_client_session_cls: MagicMock,
        conn: StdioTransportConnection,
    ) -> None:
        mock_transport_ctx = AsyncMock()
        mock_transport_ctx.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_stdio_client.return_value = mock_transport_ctx

        mock_session = _mock_session()
        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client_session_cls.return_value = mock_session_ctx

        # Connect
        result = await conn.connect()
        assert result is mock_session
        assert conn.state == ConnectionState.CONNECTED
        assert conn.is_connected is True

        # Disconnect
        await conn.disconnect()
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.is_connected is False
        assert conn._session is None
        assert conn._session_context is None
        assert conn._transport_context is None
