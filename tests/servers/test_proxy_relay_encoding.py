"""Canonical wire bytes are reused from proxy sizing through relay send."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gobby.servers.websocket.proxy_relay import ProxyHub, SocketRelay
from gobby.terminals import ws_protocol

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_unfragmented_proxy_event_encodes_once_and_counts_wire_bytes() -> None:
    event = {
        "type": "terminal_output",
        "terminal_id": "terminal-1",
        "attachment_id": "attachment-1",
        "data": "café",
    }
    sent: list[str] = []
    wire_sent = asyncio.Event()

    async def send(raw: str) -> None:
        sent.append(raw)
        wire_sent.set()

    relay = SocketRelay(websocket=SimpleNamespace(send=send, close=AsyncMock()), close=AsyncMock())
    hub = ProxyHub(owner=object())
    canonical_json = ws_protocol.canonical_json

    with (
        patch.object(hub, "relay_for", return_value=relay),
        patch.object(ws_protocol, "canonical_json", wraps=canonical_json) as sizing,
        patch(
            "gobby.servers.websocket.proxy_relay.canonical_json",
            wraps=canonical_json,
        ) as queue_encode,
    ):
        assert await hub.emit_event(object(), event, message_seq=1) is None

    expected = ws_protocol.canonical_json(event)
    assert sizing.call_count + queue_encode.call_count == 1
    assert len(relay.frame_q) == 1
    item = relay.frame_q[0]
    assert item.raw.encode("utf-8") == expected
    assert item.size == len(expected)
    assert relay.frame_bytes == len(expected)
    relay.start()
    try:
        await asyncio.wait_for(wire_sent.wait(), timeout=5)
    finally:
        await relay.shutdown("test_complete")
    assert [raw.encode("utf-8") for raw in sent] == [expected]


@pytest.mark.asyncio
async def test_fragmented_proxy_event_sends_canonical_frames_and_preserves_accounting() -> None:
    event = {
        "type": "terminal_output",
        "terminal_id": "terminal-1",
        "attachment_id": "attachment-1",
        "data": "é" * 500,
    }
    sent: list[str] = []
    all_sent = asyncio.Event()
    expected_count = 0

    async def send(raw: str) -> None:
        sent.append(raw)
        if len(sent) == expected_count:
            all_sent.set()

    relay = SocketRelay(websocket=SimpleNamespace(send=send, close=AsyncMock()), close=AsyncMock())
    hub = ProxyHub(owner=object())
    with (
        patch.object(hub, "relay_for", return_value=relay),
        patch.object(ws_protocol, "TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES", 512),
    ):
        assert await hub.emit_event(object(), event, message_seq=7) is None

    frames = list(relay.frame_q)
    assert len(frames) > 1
    assert all(item.payload["type"] == "terminal_ws_fragment" for item in frames)
    assert relay.frame_bytes == sum(item.size for item in frames)
    expected_count = len(frames)
    relay.start()
    try:
        await asyncio.wait_for(all_sent.wait(), timeout=5)
    finally:
        await relay.shutdown("test_complete")

    assert [raw.encode("utf-8") for raw in sent] == [
        ws_protocol.canonical_json(item.payload) for item in frames
    ]
    assert b"".join(base64.b64decode(item.payload["payload"]) for item in frames) == (
        ws_protocol.canonical_json(event)
    )
