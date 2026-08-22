"""Tests for stdio transport connection.

Exercises the real StdioTransportConnection code paths. The MCP SDK's
``stdio_client`` (subprocess I/O) and ``Client`` are replaced by fakes; the
env-var expansion helpers are tested against real os.environ.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.bundled import CHROME_DEVTOOLS_NPM_PACKAGE, resolve_runtime_stdio_args
from gobby.mcp_proxy.models import ConnectionState, MCPError, MCPServerConfig
from gobby.mcp_proxy.transports.stdio import StdioTransportConnection, _expand_args
from gobby.utils.env import expand_env_mapping, expand_env_variables
from tests.mcp_proxy.transports._support import FakeClient, recording_transport

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


class _ClientHarness:
    """Patch ``stdio_client`` and ``Client`` together, recording the lifecycle."""

    def __init__(self, **client_kwargs: Any) -> None:
        self.lifecycle: list[str] = []
        self.transport_calls: list[tuple[Any, Any]] = []
        self.clients: list[FakeClient] = []
        self.client_kwargs = client_kwargs
        self.transport_enter_error: BaseException | None = None

    def fake_stdio_client(self, params: Any, errlog: Any = sys.stderr) -> Any:
        self.transport_calls.append((params, errlog))
        return recording_transport(self.lifecycle, enter_error=self.transport_enter_error)

    def fake_client(self, transport: Any) -> FakeClient:
        client = FakeClient(transport, lifecycle=self.lifecycle, **self.client_kwargs)
        self.clients.append(client)
        return client

    def patches(self) -> Any:
        return (
            patch(
                "gobby.mcp_proxy.transports.stdio.stdio_client",
                side_effect=self.fake_stdio_client,
            ),
            patch("gobby.mcp_proxy.transports.stdio.Client", side_effect=self.fake_client),
        )

    @property
    def params(self) -> Any:
        return self.transport_calls[0][0]

    @property
    def errlog(self) -> Any:
        return self.transport_calls[0][1]


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
# Environment variable expansion — expand_env_variables
# ===========================================================================


class TestExpandEnvVar:
    def test_plain_text_unchanged(self) -> None:
        assert expand_env_variables("plain text") == "plain text"

    def test_empty_string(self) -> None:
        assert expand_env_variables("") == ""

    def test_existing_var_expanded(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert expand_env_variables("${MY_VAR}") == "hello"

    def test_missing_var_no_default_unchanged(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "NONEXISTENT_XYZ"}
        with patch.dict(os.environ, env_clean, clear=True):
            assert expand_env_variables("${NONEXISTENT_XYZ}") == "${NONEXISTENT_XYZ}"

    def test_missing_var_with_default(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "MISSING_ABC"}
        with patch.dict(os.environ, env_clean, clear=True):
            assert expand_env_variables("${MISSING_ABC:-fallback}") == "fallback"

    def test_existing_var_ignores_default(self) -> None:
        with patch.dict(os.environ, {"PRESENT": "real"}):
            assert expand_env_variables("${PRESENT:-fallback}") == "real"

    def test_empty_var_uses_default(self) -> None:
        with patch.dict(os.environ, {"EMPTY_V": ""}):
            assert expand_env_variables("${EMPTY_V:-fallback}") == "fallback"

    def test_empty_default_string(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "NOPE"}
        with patch.dict(os.environ, env_clean, clear=True):
            assert expand_env_variables("${NOPE:-}") == ""

    def test_multiple_vars_in_one_string(self) -> None:
        with patch.dict(os.environ, {"HOST": "localhost", "PORT": "8080"}):
            assert expand_env_variables("${HOST}:${PORT}") == "localhost:8080"

    def test_mixed_vars_and_plain_text(self) -> None:
        with patch.dict(os.environ, {"DB": "mydb"}):
            assert expand_env_variables("postgres://${DB}/data") == "postgres://mydb/data"

    def test_var_with_underscores_and_digits(self) -> None:
        with patch.dict(os.environ, {"MY_VAR_2": "works"}):
            assert expand_env_variables("${MY_VAR_2}") == "works"


# ===========================================================================
# _expand_env_dict
# ===========================================================================


class TestExpandEnvDict:
    def test_none_returns_none(self) -> None:
        assert expand_env_mapping(None) is None

    def test_empty_dict(self) -> None:
        assert expand_env_mapping({}) == {}

    def test_expands_values(self) -> None:
        with patch.dict(os.environ, {"SECRET": "s3cr3t"}):
            result = expand_env_mapping({"API_KEY": "${SECRET}"})
            assert result == {"API_KEY": "s3cr3t"}

    def test_keys_not_expanded(self) -> None:
        with patch.dict(os.environ, {"K": "v"}):
            result = expand_env_mapping({"${K}": "literal"})
            # Key is still "${K}" — only values are expanded
            assert result == {"${K}": "literal"}

    def test_multiple_entries(self) -> None:
        with patch.dict(os.environ, {"A": "1", "B": "2"}):
            result = expand_env_mapping({"x": "${A}", "y": "${B}", "z": "plain"})
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


class TestResolveRuntimeStdioArgs:
    @patch(
        "gobby.mcp_proxy.bundled.resolve_chrome_devtools_executable_path",
        return_value="/tmp/chrome",
    )
    def test_injects_chrome_executable_at_runtime(self, _mock_path: MagicMock) -> None:
        args = resolve_runtime_stdio_args(
            "chrome-devtools",
            ["-y", CHROME_DEVTOOLS_NPM_PACKAGE, "--no-usage-statistics"],
        )

        assert args == [
            "-y",
            CHROME_DEVTOOLS_NPM_PACKAGE,
            "--no-usage-statistics",
            "--executable-path=/tmp/chrome",
        ]

    @patch(
        "gobby.mcp_proxy.bundled.resolve_chrome_devtools_executable_path",
        return_value="/tmp/new-chrome",
    )
    def test_replaces_persisted_chrome_executable_arg(self, _mock_path: MagicMock) -> None:
        args = resolve_runtime_stdio_args(
            "chrome-devtools",
            [
                "-y",
                CHROME_DEVTOOLS_NPM_PACKAGE,
                "--executable-path=/tmp/old-chrome",
                "--no-usage-statistics",
            ],
        )

        assert "--executable-path=/tmp/old-chrome" not in args
        assert "--executable-path=/tmp/new-chrome" in args

    @patch(
        "gobby.mcp_proxy.bundled.resolve_chrome_devtools_executable_path",
        return_value=None,
    )
    def test_does_not_pin_package_version_at_runtime(self, _mock_path: MagicMock) -> None:
        args = resolve_runtime_stdio_args(
            "chrome-devtools",
            ["-y", "chrome-devtools-mcp@latest", "--no-usage-statistics"],
        )

        assert args == ["-y", "chrome-devtools-mcp@latest", "--no-usage-statistics"]


# ===========================================================================
# Construction & initial state
# ===========================================================================


class TestStdioInit:
    def test_initial_state(self, conn: StdioTransportConnection) -> None:
        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.session is None
        assert conn._client_context is None
        assert conn._stdio_errlog_handle is None
        assert not conn.is_connected

    def test_config_stored(self, conn: StdioTransportConnection, config: MCPServerConfig) -> None:
        assert conn.config is config


class TestStdioConnectAlreadyConnected:
    async def test_returns_existing_session(self, conn: StdioTransportConnection) -> None:
        session = AsyncMock()
        conn._state = ConnectionState.CONNECTED
        conn._session = session

        assert await conn.connect() is session


# ===========================================================================
# Connect success
# ===========================================================================


class TestStdioConnectSuccess:
    async def test_full_connect(self, conn: StdioTransportConnection) -> None:
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch:
            session = await conn.connect()

        assert conn.state == ConnectionState.CONNECTED
        assert session is harness.clients[0].session
        client_context: object = conn._client_context
        assert client_context is harness.clients[0]
        assert conn._consecutive_failures == 0
        assert harness.lifecycle == ["streams-open", "transport-enter", "handshake"]

    async def test_connect_creates_stdio_server_parameters(self) -> None:
        conn = StdioTransportConnection(
            _make_config(
                command="python",
                args=["-m", "server", "${PORT_ARG:-8080}"],
                env={"TOKEN": "${MISSING_TOKEN:-fallback}", "PLAIN": "x"},
            )
        )
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORT_ARG", None)
            os.environ.pop("MISSING_TOKEN", None)
            await conn.connect()

        assert harness.params.command == "python"
        assert harness.params.args == ["-m", "server", "8080"]
        assert harness.params.env == {"TOKEN": "fallback", "PLAIN": "x"}

    async def test_connect_with_none_args_uses_empty_list(self) -> None:
        conn = StdioTransportConnection(_make_config(args=None))
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch:
            await conn.connect()

        assert harness.params.args == []

    async def test_connect_uses_sys_stderr_without_errlog_path(
        self, conn: StdioTransportConnection
    ) -> None:
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch:
            await conn.connect()

        assert harness.errlog is sys.stderr
        assert conn._stdio_errlog_handle is None

    async def test_connect_uses_configured_errlog_path(self, tmp_path: Path) -> None:
        errlog_path = tmp_path / "logs" / "mcp-client.log"
        conn = StdioTransportConnection(_make_config(), stdio_errlog_path=str(errlog_path))
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch:
            await conn.connect()
            handle = conn._stdio_errlog_handle
            assert handle is not None
            assert harness.errlog is handle
            assert Path(handle.name) == errlog_path
            await conn.disconnect()

        assert handle.closed
        assert conn._stdio_errlog_handle is None

    @pytest.mark.parametrize(
        ("name", "command", "env", "expected_env"),
        [
            (
                "brave-search",
                "npx",
                None,
                {"npm_config_prefer_offline": "true"},
            ),
            (
                "brave-search",
                "npx",
                {"npm_config_prefer_offline": "false", "KEY": "value"},
                {"npm_config_prefer_offline": "false", "KEY": "value"},
            ),
            ("custom-server", "npx", {"KEY": "value"}, {"KEY": "value"}),
            ("brave-search", "node", None, None),
        ],
        ids=["bundled-npx", "explicit-override", "custom-npx", "bundled-non-npx"],
    )
    async def test_connect_configures_prefer_offline_only_for_bundled_npx(
        self,
        name: str,
        command: str,
        env: dict[str, str] | None,
        expected_env: dict[str, str] | None,
    ) -> None:
        conn = StdioTransportConnection(_make_config(name=name, command=command, args=[], env=env))
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch:
            await conn.connect()

        assert harness.params.command == command
        assert harness.params.args == []
        assert harness.params.env == expected_env
        assert conn.is_connected is True


# ===========================================================================
# Connect failures
# ===========================================================================


class TestStdioConnectMissingCommand:
    async def test_missing_command_raises_mcp_error(self) -> None:
        conn = StdioTransportConnection(_make_config(command=None))

        with pytest.raises(MCPError, match="Command is required"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._client_context is None


class TestStdioConnectTransportFailure:
    async def test_spawn_failure_closes_errlog_and_fails(self, tmp_path: Path) -> None:
        conn = StdioTransportConnection(
            _make_config(), stdio_errlog_path=str(tmp_path / "mcp-client.log")
        )
        harness = _ClientHarness()
        harness.transport_enter_error = OSError("spawn failed")
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch, pytest.raises(MCPError, match="spawn failed"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._client_context is None
        assert conn._stdio_errlog_handle is None
        assert harness.errlog.closed
        assert harness.clients[0].exited is False
        assert harness.lifecycle == []


class TestStdioConnectHandshakeFailure:
    async def test_handshake_failure_unwinds_transport_via_client(
        self, conn: StdioTransportConnection
    ) -> None:
        harness = _ClientHarness(handshake_error=RuntimeError("handshake boom"))
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch, pytest.raises(MCPError, match="handshake boom"):
            await conn.connect()

        assert conn.state == ConnectionState.FAILED
        assert conn._client_context is None
        assert harness.lifecycle == [
            "streams-open",
            "transport-enter",
            "streams-closed",
            "transport-exit",
        ]
        assert harness.clients[0].exited is False


class TestStdioConnectCancellation:
    async def test_handshake_cancellation_cleans_errlog_and_propagates(
        self, tmp_path: Path
    ) -> None:
        conn = StdioTransportConnection(
            _make_config(), stdio_errlog_path=str(tmp_path / "mcp-client.log")
        )
        harness = _ClientHarness(handshake_error=asyncio.CancelledError())
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch, pytest.raises(asyncio.CancelledError):
            await conn.connect()

        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._client_context is None
        assert conn._stdio_errlog_handle is None
        assert harness.errlog.closed


class TestStdioConnectMCPErrorPassthrough:
    async def test_mcp_error_not_double_wrapped(self, conn: StdioTransportConnection) -> None:
        harness = _ClientHarness(handshake_error=MCPError("already wrapped"))
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch, pytest.raises(MCPError) as excinfo:
            await conn.connect()

        assert str(excinfo.value) == "already wrapped"


class TestStdioConnectEmptyErrorMessage:
    async def test_empty_error_uses_type_name(self, conn: StdioTransportConnection) -> None:
        class SilentError(Exception):
            def __str__(self) -> str:
                return ""

        harness = _ClientHarness(handshake_error=SilentError())
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch, pytest.raises(MCPError, match="SilentError"):
            await conn.connect()


# ===========================================================================
# _cleanup_connect_attempt
# ===========================================================================


class TestStdioCleanupConnectAttempt:
    async def test_exit_error_suppressed(self, conn: StdioTransportConnection) -> None:
        client = FakeClient(recording_transport([]), exit_error=RuntimeError("exit boom"))
        conn._client_context = client  # type: ignore[assignment]

        await conn._cleanup_connect_attempt(client_entered=True)

        assert client.exited
        assert conn._client_context is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_exit_timeout_suppressed(self, conn: StdioTransportConnection) -> None:
        conn._client_context = FakeClient(recording_transport([]), exit_delay=5.0)  # type: ignore[assignment]

        with patch("gobby.mcp_proxy.transports.stdio.asyncio.wait_for", side_effect=TimeoutError):
            await conn._cleanup_connect_attempt(client_entered=True)

        assert conn._client_context is None

    async def test_not_entered_skips_exit(self, conn: StdioTransportConnection) -> None:
        client = FakeClient(recording_transport([]))
        conn._client_context = client  # type: ignore[assignment]

        await conn._cleanup_connect_attempt(client_entered=False)

        assert client.exited is False
        assert conn._client_context is None

    async def test_cancelled_exit_is_re_raised_after_reset(
        self, conn: StdioTransportConnection
    ) -> None:
        conn._client_context = FakeClient(  # type: ignore[assignment]
            recording_transport([]), exit_error=asyncio.CancelledError()
        )

        with pytest.raises(asyncio.CancelledError):
            await conn._cleanup_connect_attempt(client_entered=True)

        assert conn._client_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# Disconnect
# ===========================================================================


class TestStdioDisconnect:
    async def test_disconnect_clean_state(self, conn: StdioTransportConnection) -> None:
        await conn.disconnect()

        assert conn.state == ConnectionState.DISCONNECTED
        assert conn._client_context is None

    async def test_disconnect_exits_client(self, conn: StdioTransportConnection) -> None:
        lifecycle: list[str] = []
        client = FakeClient(recording_transport(lifecycle), lifecycle=lifecycle)
        await client.__aenter__()
        conn._client_context = client  # type: ignore[assignment]
        conn._session = client.session
        conn._state = ConnectionState.CONNECTED

        await conn.disconnect()

        assert client.exited
        assert lifecycle[-2:] == ["streams-closed", "transport-exit"]
        assert conn._client_context is None
        assert conn.session is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_timeout_handled(self, conn: StdioTransportConnection) -> None:
        conn._client_context = FakeClient(recording_transport([]), exit_delay=5.0)  # type: ignore[assignment]

        with patch("gobby.mcp_proxy.transports.stdio.asyncio.wait_for", side_effect=TimeoutError):
            await conn.disconnect()

        assert conn._client_context is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_cancel_scope_runtime_error_suppressed(
        self, conn: StdioTransportConnection, caplog: pytest.LogCaptureFixture
    ) -> None:
        conn._client_context = FakeClient(  # type: ignore[assignment]
            recording_transport([]),
            exit_error=RuntimeError("Attempted to exit cancel scope in a different task"),
        )

        with caplog.at_level("WARNING", logger="gobby.mcp.client"):
            await conn.disconnect()

        assert not [r for r in caplog.records if "Error closing client" in r.message]
        assert conn._client_context is None

    async def test_other_runtime_error_logged(
        self, conn: StdioTransportConnection, caplog: pytest.LogCaptureFixture
    ) -> None:
        conn._client_context = FakeClient(  # type: ignore[assignment]
            recording_transport([]), exit_error=RuntimeError("something else")
        )

        with caplog.at_level("WARNING", logger="gobby.mcp.client"):
            await conn.disconnect()

        assert [r for r in caplog.records if "Error closing client" in r.message]
        assert conn._client_context is None

    async def test_generic_exception_handled(self, conn: StdioTransportConnection) -> None:
        conn._client_context = FakeClient(  # type: ignore[assignment]
            recording_transport([]), exit_error=ValueError("bad")
        )

        await conn.disconnect()

        assert conn._client_context is None
        assert conn.state == ConnectionState.DISCONNECTED

    async def test_cancelled_close_re_raises_after_cleanup(
        self, conn: StdioTransportConnection
    ) -> None:
        conn._client_context = FakeClient(  # type: ignore[assignment]
            recording_transport([]), exit_error=asyncio.CancelledError()
        )

        with pytest.raises(asyncio.CancelledError):
            await conn.disconnect()

        assert conn._client_context is None
        assert conn.state == ConnectionState.DISCONNECTED


# ===========================================================================
# Full lifecycle
# ===========================================================================


class TestStdioFullLifecycle:
    async def test_connect_then_disconnect(self, conn: StdioTransportConnection) -> None:
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch:
            await conn.connect()
            assert conn.is_connected
            await conn.disconnect()

        assert conn.state == ConnectionState.DISCONNECTED
        assert conn.session is None
        assert harness.lifecycle == [
            "streams-open",
            "transport-enter",
            "handshake",
            "streams-closed",
            "transport-exit",
        ]

    async def test_reconnect_after_disconnect(self, conn: StdioTransportConnection) -> None:
        harness = _ClientHarness()
        stdio_patch, client_patch = harness.patches()
        with stdio_patch, client_patch:
            await conn.connect()
            await conn.disconnect()
            await conn.connect()

        assert conn.is_connected
        assert len(harness.clients) == 2
        client_context: object = conn._client_context
        assert client_context is harness.clients[1]
