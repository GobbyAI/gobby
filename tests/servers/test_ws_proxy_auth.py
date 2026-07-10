from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from starlette.datastructures import Headers, QueryParams

from gobby.servers import _app_ui

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _AuthService:
    enabled = True

    def is_request_authenticated(self, websocket: WebSocket) -> bool:
        bearer = websocket.headers.get("Authorization") == "Bearer browser-token"
        session = websocket.cookies.get("gobby_session") == "session-token"
        return bearer or session

    def local_token(self) -> str:
        return "daemon-token"


class _FrontendWebSocket:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.headers = Headers(headers or {})
        self.cookies = cookies or {}
        self.query_params = QueryParams()
        self.closed_codes: list[int] = []
        self.accepted = False

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted = True

    async def receive(self) -> dict[str, str]:
        return {"type": "websocket.disconnect"}

    async def send_text(self, _message: str) -> None:
        pass

    async def send_bytes(self, _message: bytes) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.closed_codes.append(code)


class _BackendWebSocket:
    subprotocol: str | None = None

    async def __aenter__(self) -> _BackendWebSocket:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def send(self, _message: str | bytes) -> None:
        pass

    async def close(self) -> None:
        pass

    def __aiter__(self) -> _BackendWebSocket:
        return self

    async def __anext__(self) -> str:
        raise StopAsyncIteration


def _proxy_endpoint() -> Callable[[WebSocket, str], Awaitable[None]]:
    app = FastAPI()
    server = SimpleNamespace(
        services=SimpleNamespace(config=None),
        auth_service=_AuthService(),
    )
    _app_ui._mount_ws_proxy(app, server)
    route = next(route for route in app.routes if getattr(route, "path", None) == "/ws/{path:path}")
    return route.endpoint


async def test_proxy_rejects_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    connect_calls: list[object] = []
    monkeypatch.setattr(
        "websockets.connect",
        lambda *_args, **_kwargs: connect_calls.append(object()),
    )
    websocket = _FrontendWebSocket()

    await _proxy_endpoint()(websocket, "chat")

    assert websocket.closed_codes == [4401]
    assert connect_calls == []


async def test_proxy_cookie_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    connections: list[dict[str, object]] = []

    def connect(target: str, **kwargs: object) -> _BackendWebSocket:
        connections.append({"target": target, **kwargs})
        return _BackendWebSocket()

    monkeypatch.setattr("websockets.connect", connect)
    websocket = _FrontendWebSocket(cookies={"gobby_session": "session-token"})

    await _proxy_endpoint()(websocket, "chat")

    assert connections == [
        {
            "target": "ws://localhost:60888/chat",
            "subprotocols": None,
            "additional_headers": [("Authorization", "Bearer daemon-token")],
        }
    ]
    assert websocket.accepted is True


async def test_proxy_bearer_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    connections: list[dict[str, object]] = []

    def connect(target: str, **kwargs: object) -> _BackendWebSocket:
        connections.append({"target": target, **kwargs})
        return _BackendWebSocket()

    monkeypatch.setattr("websockets.connect", connect)
    websocket = _FrontendWebSocket(headers={"Authorization": "Bearer browser-token"})

    await _proxy_endpoint()(websocket, "")

    assert connections[0]["additional_headers"] == [("Authorization", "Bearer daemon-token")]
