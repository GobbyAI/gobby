"""Tests for app-factory UI mounting modes."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import WebSocketRoute

from gobby.config.app import DaemonConfig
from gobby.servers import app_factory

pytestmark = pytest.mark.unit


def _server(config: DaemonConfig) -> SimpleNamespace:
    return SimpleNamespace(services=SimpleNamespace(config=config))


def test_production_ui_serves_dist_and_keeps_api_daemon_owned(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    dist_dir = web_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<main>production</main>")
    (dist_dir / "plain.txt").write_text("static")
    config = DaemonConfig(ui={"enabled": True, "mode": "production", "web_dir": str(web_dir)})
    app = FastAPI()

    app_factory._mount_production_ui(app, _server(config))

    client = TestClient(app)
    assert client.get("/").text == "<main>production</main>"
    assert client.get("/plain.txt").text == "static"
    assert client.get("/api/missing").status_code == 404


def test_dev_ui_proxies_root_to_vite_and_keeps_api_daemon_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def request(self, method: str, url: str, **_kwargs: object) -> httpx.Response:
            requests.append((method, url))
            return httpx.Response(
                200,
                content=f"proxied {url}".encode(),
                headers={"content-type": "text/plain"},
            )

    monkeypatch.setattr(app_factory.httpx, "AsyncClient", FakeAsyncClient)
    config = DaemonConfig(ui={"enabled": True, "mode": "dev", "port": 5173})
    app = FastAPI()

    @app.get("/api/owned")
    def owned_api() -> dict[str, bool]:
        return {"owned": True}

    app_factory._mount_vite_dev_ui(app, _server(config))

    client = TestClient(app)
    assert client.get("/").text == "proxied http://localhost:5173/"
    assert client.get("/src/main.tsx?x=1").text == "proxied http://localhost:5173/src/main.tsx?x=1"
    assert client.get("/api/owned").json() == {"owned": True}
    assert client.get("/api/missing").status_code == 404
    assert requests == [
        ("GET", "http://localhost:5173/"),
        ("GET", "http://localhost:5173/src/main.tsx?x=1"),
    ]


def test_hmr_proxy_uses_dedicated_route_and_ws_remains_gobby_owned() -> None:
    config = DaemonConfig(ui={"enabled": True, "mode": "dev", "port": 5173})
    app = FastAPI()

    app_factory._mount_ws_proxy(app, _server(config))
    app_factory._mount_vite_hmr_proxy(app, _server(config))

    websocket_paths = {route.path for route in app.routes if isinstance(route, WebSocketRoute)}
    assert "/ws" in websocket_paths
    assert "/ws/{path:path}" in websocket_paths
    assert "/__vite_hmr" in websocket_paths
    assert "/__vite_hmr/{path:path}" in websocket_paths
