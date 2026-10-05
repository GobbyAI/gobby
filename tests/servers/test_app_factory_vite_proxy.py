"""Tests for the dev-mode Vite proxy: disconnects, body forwarding and client pooling."""

import asyncio
import logging
from collections.abc import AsyncIterable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers import app_factory
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

pytestmark = pytest.mark.unit


def _server(config: DaemonConfig) -> "HTTPServer":
    # The proxy mount reads only these attributes of the server.
    return cast(
        "HTTPServer",
        SimpleNamespace(
            services=SimpleNamespace(config=config),
            startup_config=config,
            bootstrap_config=BootstrapConfig(ui_port=config.ui.port),
        ),
    )


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
    upstream_urls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def request(
            self,
            method: str,
            url: str,
            *,
            content: AsyncIterable[bytes],
            **_kwargs: object,
        ) -> httpx.Response:
            _ = method
            upstream_urls.append(url)
            async for _chunk in content:
                pass
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
        response: Response = await endpoint(request, path="src/main.tsx")

    assert response.status_code == 499
    assert upstream_urls == ["http://localhost:5173/src/main.tsx"]
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
            self,
            method: str,
            url: str,
            *,
            content: AsyncIterable[bytes],
            **_kwargs: object,
        ) -> httpx.Response:
            chunks = [chunk async for chunk in content]
            forwarded["method"] = method
            forwarded["url"] = url
            forwarded["content"] = b"".join(chunks)
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


@pytest.mark.asyncio
async def test_vite_proxy_serves_concurrent_module_loads_through_one_pooled_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser boot fans out hundreds of module requests through the proxy.

    Building an httpx.AsyncClient is synchronous work (about 12-20 ms measured live), so
    constructing one per request blocks the daemon event loop and serializes every module
    load; the dev SPA then never finishes booting through the daemon port. Every request
    must share one client and all of them must be in flight upstream at once.
    """
    chunk_paths = [f"node_modules/.vite/deps/chunk-{index:04d}.js" for index in range(20)]
    constructions = 0
    in_flight = 0
    all_in_flight = asyncio.Event()

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructions
            constructions += 1

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def request(
            self,
            method: str,
            url: str,
            *,
            content: AsyncIterable[bytes],
            **_kwargs: object,
        ) -> httpx.Response:
            nonlocal in_flight
            _ = method
            async for _chunk in content:
                pass
            in_flight += 1
            if in_flight == len(chunk_paths):
                all_in_flight.set()
            await all_in_flight.wait()
            return httpx.Response(
                200, content=url.encode(), headers={"content-type": "text/javascript"}
            )

    monkeypatch.setattr(app_factory.httpx, "AsyncClient", FakeAsyncClient)

    from fastapi import FastAPI

    app = FastAPI()
    config = DaemonConfig(ui={"enabled": True, "mode": "dev", "port": 5173})
    app_factory._mount_vite_dev_ui(app, _server(config))
    endpoint = _vite_proxy_endpoint(app)

    async def receive_empty() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async with asyncio.timeout(5):
        responses: list[Response] = await asyncio.gather(
            *(
                endpoint(_make_request("GET", path, receive_empty), path=path)
                for path in chunk_paths
            )
        )

    assert [response.status_code for response in responses] == [200] * len(chunk_paths)
    assert [bytes(response.body) for response in responses] == [
        f"http://localhost:5173/{path}".encode() for path in chunk_paths
    ]
    assert in_flight == len(chunk_paths)
    assert constructions == 1


@pytest.mark.asyncio
async def test_app_shutdown_closes_the_pooled_vite_proxy_client(
    session_storage: SessionManager,
) -> None:
    """The pooled proxy client lives with the app and is closed by the lifespan teardown."""
    server = create_http_server(port=60887, test_mode=True, session_manager=session_storage)
    server.app.state.hook_manager = MagicMock()
    server.app.state.hook_manager.shutdown_async = AsyncMock()
    proxy_client = httpx.AsyncClient()
    server.app.state.vite_proxy_client = proxy_client

    async with server.app.router.lifespan_context(server.app):
        assert proxy_client.is_closed is False

    assert proxy_client.is_closed is True
