"""Fault injection must outlive the proxy's ordinary WebSocket handshake budget."""

import asyncio
from functools import partial
from typing import cast
from unittest.mock import Mock

import httpx
import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from tests.e2e import test_terminal_client_stack as stack
from tests.e2e.conftest import DaemonInstance


@pytest.mark.asyncio
async def test_http_fault_can_finish_after_proxy_handshake_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scale the real server's default down; the callback still spans that
    # deadline, without requiring a daemon, native client or ten-second wait.
    monkeypatch.setattr(stack, "serve", partial(serve, open_timeout=0.05))
    wire = stack.ClientWire(cast(DaemonInstance, Mock(spec=DaemonInstance)))

    async def delayed_response(_connection: ServerConnection, request: Request) -> Response:
        assert request.path == "/api/admin/config"
        release = asyncio.Event()
        timer = asyncio.get_running_loop().call_later(0.15, release.set)
        try:
            await asyncio.wait_for(release.wait(), timeout=1.0)
        finally:
            timer.cancel()
        return Response(200, "OK", Headers({"Content-Length": "4"}), b"done")

    monkeypatch.setattr(wire, "http", delayed_response)
    async with wire.running(), httpx.AsyncClient(timeout=2.0) as client:
        response = await client.get(wire.url + "/api/admin/config")
    assert response.status_code == 200
    assert response.content == b"done"
