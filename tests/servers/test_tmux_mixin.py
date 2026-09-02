"""Tests for WebSocket TmuxMixin handlers."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.server import WebSocketServer

pytestmark = pytest.mark.unit


class MockWebSocket:
    def __init__(self, user_id: str = "test-user") -> None:
        self.user_id = user_id
        self.latency = 0.1
        self.sent_messages: list[str] = []
        self.closed = False
        self.subscriptions: set[str] = {"*"}
        self.remote_address = ("127.0.0.1", 12345)

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def last_message(self) -> dict:
        return json.loads(self.sent_messages[-1])

    def all_messages(self) -> list[dict]:
        return [json.loads(m) for m in self.sent_messages]

    def messages_of_type(self, msg_type: str) -> list[dict]:
        return [m for m in self.all_messages() if m.get("type") == msg_type]


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    return config


@pytest.fixture
def mock_mcp_manager() -> MagicMock:
    return MagicMock()


@pytest.fixture
def server(mock_config: MagicMock, mock_mcp_manager: MagicMock) -> WebSocketServer:
    return WebSocketServer(mock_config, mock_mcp_manager, AsyncMock(return_value="test-user"))


class TestTmuxMixinInit:
    """Test TmuxMixin initialization."""

    def test_tmux_bridge_initialized(self, server: WebSocketServer) -> None:
        assert hasattr(server, "_tmux_bridge")
        assert hasattr(server, "_tmux_pending")
        assert hasattr(server, "_tmux_mgr_gobby")
        assert hasattr(server, "_tmux_mgr_default")
        assert hasattr(server, "_tmux_client_bridges")
        assert hasattr(server, "lease_registry")

    def test_gobby_manager_has_socket(self, server: WebSocketServer) -> None:
        assert server._tmux_mgr_gobby.config.socket_name == "gobby"

    def test_default_manager_no_socket(self, server: WebSocketServer) -> None:
        assert server._tmux_mgr_default.config.socket_name == ""


class TestTmuxClientCleanup:
    """Test client disconnect cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_empty(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        result = await server._cleanup_tmux_client(ws)
        assert result is None
        assert ws not in server._tmux_client_bridges

    @pytest.mark.asyncio
    async def test_cleanup_with_bridges(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        server._tmux_client_bridges[ws] = {"stream-1", "stream-2"}
        await server._cleanup_tmux_client(ws)
        assert ws not in server._tmux_client_bridges


class TestTerminalInputRouting:
    """Legacy PTY-bridge input routing is retired; a run id goes to the agent lookup."""

    @pytest.mark.asyncio
    async def test_input_reaches_the_run_lookup_for_a_run_id(self, server: WebSocketServer) -> None:
        ws = MockWebSocket()
        mock_session_mgr = MagicMock()
        server.session_manager = mock_session_mgr
        mock_arm = MagicMock()
        mock_arm.get.return_value = None
        run_id = str(uuid.uuid4())
        with patch("gobby.storage.agents.LocalAgentRunManager", return_value=mock_arm):
            await server._handle_terminal_input(ws, {"run_id": run_id, "data": "x"})
        mock_arm.get.assert_called_once_with(run_id)

    @pytest.mark.asyncio
    async def test_input_for_a_detached_tmux_id_never_reaches_the_run_lookup(
        self, server: WebSocketServer
    ) -> None:
        """A tmux streaming id is not a uuid, so the runs query would raise on it.

        The web terminal answers tmux's DA and DSR queries as terminal_input, so
        a reply that lands after its attachment detached arrives with a run_id
        the uuid-keyed lookup cannot parse (#20803).
        """
        ws = MockWebSocket()
        # A database has to be reachable, or the handler would stop short of
        # the lookup for a reason that has nothing to do with the id.
        server.session_manager = SimpleNamespace(db=object())

        def refuse_db(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a tmux streaming id must never reach the agent-run lookup")

        with patch("gobby.storage.agents.LocalAgentRunManager", side_effect=refuse_db):
            await server._handle_terminal_input(
                ws, {"run_id": "tmux-68bd19945ce3", "data": "\x1b[?1;2c"}
            )

        assert ws.sent_messages == []
