"""``terminal_kill`` destroys orphaned and detached owned rows (#22067)."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.servers.websocket.terminal_ws_create import TerminalCreateMixin
from gobby.terminals.host_client import HostEpochChangedError
from tests.servers.terminal_fakes import MockWebSocket
from tests.servers.test_terminal_ws_golden import TERMINAL_ID, _server

pytestmark = pytest.mark.unit


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
    cast(Any, server).broadcast_terminal_event = broadcast

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
async def test_kill_answers_a_runtime_failure_instead_of_dropping_the_reply() -> None:
    server, manager, runtime = _server(backend="native")
    manager.row.state = "orphaned"

    async def terminate(row: Any, grace_seconds: float) -> None:
        raise HostEpochChangedError("host_epoch_changed")

    cast(Any, runtime).terminate = terminate
    broadcast = AsyncMock()
    cast(Any, server).broadcast_terminal_event = broadcast

    reply = await _kill(server, TERMINAL_ID)

    assert manager.row.state == "orphaned"
    broadcast.assert_not_awaited()
    assert reply == {
        "type": "terminal_kill_result",
        "success": False,
        "terminal_id": TERMINAL_ID,
        "request_id": "kill-1",
        "code": "kill_failed",
        "reason": "host_epoch_changed",
    }
