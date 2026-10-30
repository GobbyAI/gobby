"""Expected terminal proxy socket disconnects during client teardown."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError

from gobby.servers.websocket.proxy_relay import ProxyHub, SocketRelay
from gobby.terminals.leases import TerminalLeaseRegistry

pytestmark = pytest.mark.unit


async def test_send_after_socket_close_uses_ws_close_without_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def send(_raw: str) -> None:
        raise ConnectionClosedError(None, None)

    closed: list[str] = []

    async def on_close(reason: str) -> None:
        closed.append(reason)

    relay = SocketRelay(
        websocket=SimpleNamespace(send=send, close=AsyncMock()),
        close=on_close,
    )
    relay.start()
    sender = relay._sender
    assert sender is not None
    assert relay.enqueue_frame({"type": "terminal_output"}, b"output") is None

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(sender, timeout=5)

    assert relay.closed is True
    assert closed == ["ws_close"]
    assert "terminal proxy relay send failed" not in caplog.text


async def test_unexpected_send_failure_keeps_error_log_and_lag_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def send(_raw: str) -> None:
        raise RuntimeError("send failed")

    closed: list[str] = []

    async def on_close(reason: str) -> None:
        closed.append(reason)

    relay = SocketRelay(
        websocket=SimpleNamespace(send=send, close=AsyncMock()),
        close=on_close,
    )
    relay.start()
    sender = relay._sender
    assert sender is not None
    assert relay.enqueue_frame({"type": "terminal_output"}, b"output") is None

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(sender, timeout=5)

    assert closed == ["proxy_lag"]
    assert "terminal proxy relay send failed" in caplog.text


async def test_ws_close_during_pending_lifecycle_emit_leaves_the_registry_publishing() -> None:
    registry = TerminalLeaseRegistry()
    hub = ProxyHub(SimpleNamespace(_leases=lambda: registry))
    sending = asyncio.Event()

    class _Socket:
        async def send(self, _raw: str) -> None:
            sending.set()
            await asyncio.Event().wait()

        close = AsyncMock()

    websocket = _Socket()
    emit = asyncio.create_task(
        hub.emit_lifecycle(websocket, {"type": "terminal_event", "event": "exited"})
    )
    await asyncio.wait_for(sending.wait(), timeout=5)
    await hub.drop_socket(websocket, "ws_close")
    await asyncio.wait_for(emit, timeout=5)

    async def publish(_stamped: dict[str, object]) -> None:
        return None

    stamped = await asyncio.wait_for(
        registry.publish_lifecycle({"type": "terminal_event", "event": "pending"}, publish),
        timeout=5,
    )
    assert stamped["type"] == "terminal_event"
