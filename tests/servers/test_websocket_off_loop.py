"""Disconnects cancel socket reads without abandoning preceding mutations."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.server import WebSocketServer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "mutation,read",
    [
        ({"type": "workspace_op", "op": "workspace.rename"}, {"type": "workspace_snapshot"}),
        (
            {"type": "terminal_kill", "terminal_id": "term-1"},
            {"type": "terminal_resize", "terminal_id": "term-1"},
        ),
        (
            {"type": "terminal_kill", "terminal_id": "term-1"},
            {"type": "terminal_detach", "terminal_id": "term-1"},
        ),
    ],
)
async def test_disconnect_preserves_durable_predecessor(
    mutation: dict[str, str], read: dict[str, str]
) -> None:
    server = WebSocketServer(MagicMock(), MagicMock(), AsyncMock(return_value="user-1"))
    socket = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()
    completed: list[str] = []

    async def handle(_socket: Any, message: str, *, blocks_connection: bool) -> None:
        assert not blocks_connection
        payload = json.loads(message)
        if payload == mutation:
            started.set()
            await release.wait()
        completed.append(payload["type"])

    with patch.object(server, "_handle_message", side_effect=handle):
        server._start_off_loop(socket, json.dumps(mutation))
        await asyncio.wait_for(started.wait(), timeout=1)
        durable = next(iter(server._off_loop_durable))
        server._start_off_loop(socket, json.dumps(read))
        # The waiter enters its predecessor await before the socket disappears.
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(server._cancel_off_loop(socket), timeout=1)
            assert not durable.done()
            assert completed == []
        finally:
            release.set()
            await asyncio.gather(durable, return_exceptions=True)

    assert completed == [mutation["type"]]
    assert not durable.cancelled()


@pytest.mark.parametrize(
    "first,read,last",
    [
        (
            {"type": "workspace_op", "op": "workspace.rename", "name": "first"},
            {"type": "workspace_snapshot"},
            {"type": "workspace_op", "op": "workspace.rename", "name": "last"},
        ),
        (
            {"type": "terminal_kill", "terminal_id": "term-1", "request_id": "first"},
            {"type": "terminal_resize", "terminal_id": "term-1"},
            {"type": "terminal_kill", "terminal_id": "term-1", "request_id": "last"},
        ),
    ],
)
async def test_cancelled_middle_read_preserves_mutation_order(
    first: dict[str, str], read: dict[str, str], last: dict[str, str]
) -> None:
    server = WebSocketServer(MagicMock(), MagicMock(), AsyncMock(return_value="user-1"))
    socket = MagicMock()
    entered = asyncio.Event()
    release = asyncio.Event()
    completed: list[dict[str, str]] = []

    async def handle(_socket: Any, message: str, *, blocks_connection: bool) -> None:
        assert not blocks_connection
        payload = json.loads(message)
        if payload == first:
            entered.set()
            await release.wait()
        completed.append(payload)

    with patch.object(server, "_handle_message", side_effect=handle):
        server._start_off_loop(socket, json.dumps(first))
        await asyncio.wait_for(entered.wait(), timeout=1)
        server._start_off_loop(socket, json.dumps(read))
        server._start_off_loop(socket, json.dumps(last))
        durable = tuple(server._off_loop_durable)
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(server._cancel_off_loop(socket), timeout=1)
            await asyncio.sleep(0)
            assert completed == []
            assert all(not task.done() for task in durable)
        finally:
            release.set()
            await asyncio.gather(*durable, return_exceptions=True)
    assert completed == [first, last]


async def test_input_and_control_complete_while_workspace_attach_waits() -> None:
    server = WebSocketServer(MagicMock(), MagicMock(), AsyncMock(return_value="user-1"))
    socket = MagicMock()
    attaching = asyncio.Event()
    release = asyncio.Event()
    replies = asyncio.Queue[str]()

    async def handle(_socket: Any, message: str, *, blocks_connection: bool) -> None:
        assert not blocks_connection
        kind = json.loads(message)["type"]
        if kind == "workspace_attach":
            attaching.set()
            await release.wait()
        replies.put_nowait(kind)

    with patch.object(server, "_handle_message", side_effect=handle):
        server._start_off_loop(socket, json.dumps({"type": "workspace_attach"}))
        await asyncio.wait_for(attaching.wait(), timeout=1)
        attach = next(iter(server._off_loop_tasks[socket]))
        try:
            for kind in ("terminal_input", "terminal_take_control"):
                server._start_off_loop(socket, json.dumps({"type": kind, "terminal_id": "term-1"}))
            assert await asyncio.wait_for(replies.get(), timeout=1) == "terminal_input"
            assert await asyncio.wait_for(replies.get(), timeout=1) == "terminal_take_control"
            assert not attach.done()
        finally:
            release.set()
            await attach
            await server._cancel_off_loop(socket)
    assert replies.get_nowait() == "workspace_attach"
