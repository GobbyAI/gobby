"""``terminal_kill`` destroys orphaned rows and detached external tmux rows (#22067)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.servers.websocket.terminal_ws_create import TerminalCreateMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager, tmux_locator_key
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests.fixtures.isolated_checkout import patch_local_machine_id
from tests.servers.test_terminal_ws_golden import TERMINAL_ID, _server
from tests.servers.test_tmux_mixin import MockWebSocket
from tests.storage.test_terminals import LOCAL_MACHINE_ID

pytestmark = pytest.mark.unit

DEFAULT_SOCKET = "/private/tmp/tmux-501/default"


async def _kill(server: Any, terminal_id: str) -> dict[str, Any]:
    websocket = MockWebSocket()
    await TerminalCreateMixin._handle_terminal_kill(
        server,
        websocket,
        {"type": "terminal_kill", "terminal_id": terminal_id, "request_id": "kill-1"},
    )
    return websocket.last_message()


@pytest.mark.asyncio
async def test_kill_orphaned_row_marks_exited_and_broadcasts() -> None:
    server, manager, runtime = _server(backend="native")
    manager.row.state = "orphaned"
    terminated: list[tuple[str, float]] = []

    async def terminate(row: Any, grace_seconds: float) -> None:
        terminated.append((row.state, grace_seconds))

    cast(Any, runtime).terminate = terminate
    broadcast = AsyncMock()
    cast(Any, server).broadcast_tmux_session_event = broadcast

    reply = await _kill(server, TERMINAL_ID)

    assert terminated == [("orphaned", 1.0)]
    assert manager.row.state == "exited"
    broadcast.assert_awaited_once_with("killed", terminal_id=TERMINAL_ID)
    assert reply == {
        "type": "terminal_kill_result",
        "success": True,
        "terminal_id": TERMINAL_ID,
        "request_id": "kill-1",
    }


@pytest.mark.asyncio
async def test_kill_detached_external_tmux_row_kills_on_its_socket(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interactive default-socket session is an external row; the kill targets that socket."""
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)
    manager = TerminalManager(temp_db)
    row = manager.upsert_external(
        project_id=sample_project["id"],
        backend="tmux",
        locator={
            "socket_path": DEFAULT_SOCKET,
            "server_pid": 6051,
            "server_start_time": 1787385464,
            "pane_id": "%7",
        },
        locator_key=tmux_locator_key(
            socket_path=DEFAULT_SOCKET, server_pid=6051, server_start_time=1787385464, pane_id="%7"
        ),
        session_name="scratch",
        title="scratch",
    )
    killed: list[tuple[str | None, str]] = []

    async def fake_kill(
        self: TmuxSessionManager, name: str, *, missing_ok: bool = False, timeout: float = 5.0
    ) -> bool:
        killed.append((self.config.socket_path, name))
        return True

    monkeypatch.setattr(TmuxSessionManager, "kill_session", fake_kill)
    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))
    server.terminal_manager = manager
    runtime = TmuxTerminalRuntime(TmuxSessionManager(TmuxConfig(socket_name="gobby")))
    server.terminal_runtime_registry = MagicMock(resolve=lambda _backend: runtime)
    broadcast = AsyncMock()
    cast(Any, server).broadcast_tmux_session_event = broadcast

    reply = await _kill(server, row.id)

    assert killed == [(DEFAULT_SOCKET, "scratch")]
    refreshed = manager.get(row.id)
    assert refreshed is not None and refreshed.state == "exited"
    broadcast.assert_awaited_once_with("killed", terminal_id=row.id)
    assert reply["success"] is True
