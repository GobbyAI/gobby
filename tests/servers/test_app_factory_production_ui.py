"""Regression tests for `_mount_production_ui` (GH #10).

Asserts the production UI mount works against an installed-from-wheel
layout (only ``dist/`` exists, no ``package.json``) so that
``GET /`` returns ``index.html`` instead of 404.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.app_factory import _mount_production_ui

pytestmark = pytest.mark.unit


def _make_dist_only_web_dir(root: Path) -> Path:
    web = root / "web"
    dist = web / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>UI</body></html>")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("// app bundle")
    return web


def _server_stub(web_dir: Path) -> SimpleNamespace:
    config = SimpleNamespace(ui=SimpleNamespace(web_dir=str(web_dir)))
    return SimpleNamespace(services=SimpleNamespace(config=config))


def test_mount_production_ui_serves_index_on_root(tmp_path: Path) -> None:
    web_dir = _make_dist_only_web_dir(tmp_path)
    app = FastAPI()
    _mount_production_ui(app, _server_stub(web_dir))  # type: ignore[arg-type]

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<html>" in resp.text


def test_mount_production_ui_serves_assets(tmp_path: Path) -> None:
    web_dir = _make_dist_only_web_dir(tmp_path)
    app = FastAPI()
    _mount_production_ui(app, _server_stub(web_dir))  # type: ignore[arg-type]

    client = TestClient(app)
    resp = client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "app bundle" in resp.text


def test_mount_production_ui_falls_back_to_index_for_spa_routes(tmp_path: Path) -> None:
    web_dir = _make_dist_only_web_dir(tmp_path)
    app = FastAPI()
    _mount_production_ui(app, _server_stub(web_dir))  # type: ignore[arg-type]

    client = TestClient(app)
    resp = client.get("/some/spa/route")
    assert resp.status_code == 200
    assert "<html>" in resp.text


def test_mount_production_ui_no_op_when_dist_missing(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package.json").write_text("{}")

    app = FastAPI()
    _mount_production_ui(app, _server_stub(web_dir))  # type: ignore[arg-type]

    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 404
