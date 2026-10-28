"""Abrupt peer loss terminates stream writers before asyncio warning floods."""

from __future__ import annotations

import asyncio
import logging

import pytest
import uvicorn
import websockets
from fastapi import FastAPI, WebSocket
from websockets.exceptions import ConnectionClosedError

from gobby.servers.websocket.asgi_adapter import ASGIWebSocketAdapter
from gobby.servers.websocket_protocol import WebSocketProtocol

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("paused", [False, True])
async def test_abrupt_disconnect_stops_stream_without_socket_warning(
    paused: bool, caplog: pytest.LogCaptureFixture
) -> None:
    lost = asyncio.Event()
    resume = asyncio.Event()
    finished = asyncio.Event()
    connected: list[WebSocketProtocol] = []
    delivered: list[str] = []
    adapters: list[ASGIWebSocketAdapter] = []

    class ObservedProtocol(WebSocketProtocol):
        def connection_lost(self, exc: Exception | None) -> None:
            super().connection_lost(exc)
            lost.set()

    app = FastAPI()

    @app.websocket("/ws")
    async def stream(websocket: WebSocket) -> None:
        adapter = ASGIWebSocketAdapter(websocket, user_id="isolated-test")
        adapters.append(adapter)
        await adapter.accept()
        await adapter.send("ready")
        delivered.append("ready")
        await resume.wait()
        try:
            for index in range(20):
                await adapter.send(f"frame-{index}")
                delivered.append(f"frame-{index}")
        except ConnectionClosedError:
            pass
        finally:
            finished.set()

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", ws=ObservedProtocol,
        log_level="warning", log_config=None, access_log=False,
    ))
    serving = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0)
        sockets = server.servers[0].sockets
        assert sockets is not None
        port = sockets[0].getsockname()[1]
        with caplog.at_level(logging.WARNING, logger="asyncio"):
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as client:
                assert await client.recv() == "ready"
                connected.extend(
                    protocol for protocol in server.server_state.connections
                    if isinstance(protocol, WebSocketProtocol)
                )
                assert len(connected) == 1
                if paused:
                    connected[0].pause_writing()
                # Abort TCP without a WebSocket close frame. The writer has no
                # receive loop to consume the queued ASGI disconnect notification.
                client.transport.abort()
                await asyncio.wait_for(lost.wait(), timeout=2)
                resume.set()
                await asyncio.wait_for(finished.wait(), timeout=2)
        assert delivered == ["ready"]
        assert adapters[0].disconnected
        assert not any("socket.send() raised exception" in record.message for record in caplog.records)
    finally:
        resume.set()
        for protocol in connected:
            protocol.resume_writing()
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
