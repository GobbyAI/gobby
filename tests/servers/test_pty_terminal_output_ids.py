"""PTY terminal_output frames carry the row id and attachment id separately."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.runner_broadcasting import _emit_pty_terminal_output
from tests.servers.websocket.test_broadcast import FakeBroadcaster, _make_ws, _sent_message

pytestmark = pytest.mark.unit

TERMINAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ATTACHMENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class _BridgeServer(FakeBroadcaster):
    async def _tmux_bridge_for(self, attachment_id: object) -> Any | None:
        if attachment_id == ATTACHMENT_ID:
            return SimpleNamespace(terminal_id=TERMINAL_ID)
        return None


@pytest.mark.asyncio
async def test_pty_output_callback_emits_row_and_attachment_ids() -> None:
    server = _BridgeServer()
    ws = _make_ws(subscriptions={"terminal_output"})
    server.clients[ws] = {}
    await _emit_pty_terminal_output(server, ATTACHMENT_ID, "ready.\n")
    msg = _sent_message(ws)
    assert msg["type"] == "terminal_output"
    assert msg["terminal_id"] == TERMINAL_ID
    assert msg["attachment_id"] == ATTACHMENT_ID
    assert msg["data"] == "ready.\n"


@pytest.mark.asyncio
async def test_pty_output_callback_without_bridge_leaves_attachment_id_null() -> None:
    server = FakeBroadcaster()
    ws = _make_ws(subscriptions={"terminal_output"})
    server.clients[ws] = {}
    await _emit_pty_terminal_output(server, "run-1", "hello")
    msg = _sent_message(ws)
    assert msg["type"] == "terminal_output"
    assert msg["terminal_id"] == "run-1"
    assert msg["attachment_id"] is None
    assert msg["data"] == "hello"
