"""PTY terminal_output frames are keyed by the run and carry no attachment."""

from __future__ import annotations

import pytest

from gobby.runner_broadcasting import _emit_pty_terminal_output
from tests.servers.websocket.test_broadcast import FakeBroadcaster, _make_ws, _sent_message

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_pty_output_callback_leaves_attachment_id_null() -> None:
    server = FakeBroadcaster()
    ws = _make_ws(subscriptions={"terminal_output"})
    server.clients[ws] = {}
    await _emit_pty_terminal_output(server, "run-1", "hello")
    msg = _sent_message(ws)
    assert msg["type"] == "terminal_output"
    assert msg["terminal_id"] == "run-1"
    assert msg["attachment_id"] is None
    assert msg["data"] == "hello"
