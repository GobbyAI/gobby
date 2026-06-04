"""Tests for the dev-mode Vite proxy's client-disconnect handling."""

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

from gobby.config.app import DaemonConfig
from gobby.servers import app_factory

pytestmark = pytest.mark.unit


def _server(config: DaemonConfig) -> SimpleNamespace:
    return SimpleNamespace(services=SimpleNamespace(config=config))


def _vite_proxy_endpoint(app: Any) -> Any:
    """Return the mounted vite_proxy endpoint callable for direct invocation."""
    for route in app.routes:
        if getattr(route, "path", None) == "/{path:path}":
            return route.endpoint
    raise AssertionError("vite_proxy catch-all route was not mounted")


def _make_request(method: str, path: str, receive: Any) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": f"/{path}",
        "raw_path": f"/{path}".encode(),
        "query_string": b"",
        "headers": [],
    }
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_vite_proxy_swallows_client_disconnect_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A client that disconnects mid-request exits early without a traceback."""
    upstream_calls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def request(self, method: str, url: str, **_kwargs: object) -> httpx.Response:
            upstream_calls.append(url)
            return httpx.Response(200, content=b"unexpected")

    monkeypatch.setattr(app_factory.httpx, "AsyncClient", FakeAsyncClient)

    from fastapi import FastAPI

    app = FastAPI()
    config = DaemonConfig(ui={"enabled": True, "mode": "dev", "port": 5173})
    app_factory._mount_vite_dev_ui(app, _server(config))
    endpoint = _vite_proxy_endpoint(app)

    async def receive_disconnect() -> dict[str, str]:
        return {"type": "http.disconnect"}

    request = _make_request("POST", "src/main.tsx", receive_disconnect)

    with caplog.at_level(logging.DEBUG, logger=app_factory.logger.name):
        response: Response | None = await endpoint(request, path="src/main.tsx")

    assert response is None
    # The disconnect must short-circuit before reaching the upstream Vite server.
    assert upstream_calls == []
    # No error-level traceback should be emitted — only a debug breadcrumb.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("client disconnected" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_vite_proxy_forwards_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully-delivered body is read once and forwarded to the upstream server."""
    forwarded: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def request(
            self, method: str, url: str, *, content: bytes, **_kwargs: object
        ) -> httpx.Response:
            forwarded["method"] = method
            forwarded["url"] = url
            forwarded["content"] = content
            return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    monkeypatch.setattr(app_factory.httpx, "AsyncClient", FakeAsyncClient)

    from fastapi import FastAPI

    app = FastAPI()
    config = DaemonConfig(ui={"enabled": True, "mode": "dev", "port": 5173})
    app_factory._mount_vite_dev_ui(app, _server(config))
    endpoint = _vite_proxy_endpoint(app)

    delivered = False

    async def receive_body() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"payload", "more_body": False}
        return {"type": "http.disconnect"}

    request = _make_request("POST", "api-ish", receive_body)
    response: Response = await endpoint(request, path="api-ish")

    assert response.status_code == 200
    assert forwarded["method"] == "POST"
    assert forwarded["url"] == "http://localhost:5173/api-ish"
    assert forwarded["content"] == b"payload"
