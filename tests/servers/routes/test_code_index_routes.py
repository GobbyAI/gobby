"""Tests for code-index HTTP routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from gobby.code_index.models import Symbol
from gobby.servers.routes.code_index import create_code_index_router

pytestmark = pytest.mark.unit


def _make_symbol(symbol_id: str = "sym-1", name: str = "handler") -> Symbol:
    return Symbol(
        id=symbol_id,
        project_id="proj-1",
        file_path="src/app.py",
        name=name,
        qualified_name=name,
        kind="function",
        language="python",
        byte_start=0,
        byte_end=10,
        line_start=1,
        line_end=1,
        content_hash="hash",
    )


@pytest.fixture
def mock_server() -> MagicMock:
    server = MagicMock()
    server.services = MagicMock()
    code_indexer = MagicMock()
    code_indexer.graph = MagicMock()
    code_indexer.graph.available = True
    code_indexer.graph.get_file_graph = AsyncMock(return_value={"nodes": [], "links": []})
    code_indexer.graph.get_file_symbols = AsyncMock(return_value={"nodes": [], "links": []})
    code_indexer.graph.get_symbol_neighbors = AsyncMock(return_value={"nodes": [], "links": []})
    code_indexer.graph.get_blast_radius_graph = AsyncMock(
        return_value={"nodes": [], "links": [], "center": "sym-1"}
    )
    code_indexer.clear_graph = AsyncMock(return_value={"success": True, "project_id": "proj-1"})
    code_indexer.rebuild_graph = AsyncMock(
        return_value={"success": True, "project_id": "proj-1", "files_processed": 0, "errors": []}
    )
    code_indexer.invalidate = AsyncMock(return_value=None)
    code_indexer.storage = MagicMock()
    code_indexer.storage.search_symbols_fts = MagicMock(return_value=[])
    code_indexer.storage.search_symbols_by_name = MagicMock(return_value=[_make_symbol()])
    code_indexer.storage.get_project_stats = MagicMock(return_value=MagicMock())
    server.services.code_indexer = code_indexer
    return server


@pytest.fixture
def client(mock_server: MagicMock) -> TestClient:
    app = FastAPI()
    app.include_router(create_code_index_router(mock_server))
    return TestClient(app)


def test_graph_overview_requires_project_id(client: TestClient) -> None:
    response = client.get("/api/code-index/graph")
    assert response.status_code == 400


def test_graph_overview_returns_data(client: TestClient, mock_server: MagicMock) -> None:
    response = client.get("/api/code-index/graph", params={"project_id": "proj-1", "limit": 25})
    assert response.status_code == 200
    mock_server.services.code_indexer.graph.get_file_graph.assert_awaited_once_with(
        "proj-1",
        limit=25,
    )


def test_search_falls_back_to_name_search(client: TestClient, mock_server: MagicMock) -> None:
    response = client.get("/api/code-index/graph/search", params={"project_id": "proj-1", "q": "handler"})
    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["id"] == "sym-1"
    assert data["results"][0]["type"] == "function"
    mock_server.services.code_indexer.storage.search_symbols_fts.assert_called_once_with(
        "handler",
        "proj-1",
        kind=None,
        file_path=None,
        limit=25,
    )
    mock_server.services.code_indexer.storage.search_symbols_by_name.assert_called_once_with(
        "handler",
        "proj-1",
        kind=None,
        file_path=None,
        limit=25,
    )


def test_blast_radius_validates_exclusive_target(client: TestClient) -> None:
    response = client.get(
        "/api/code-index/graph/blast-radius",
        params={"project_id": "proj-1", "symbol_id": "sym-1", "file_path": "src/app.py"},
    )
    assert response.status_code == 400


def test_clear_graph_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.post("/api/code-index/graph/clear", params={"project_id": "proj-1"})
    assert response.status_code == 200
    mock_server.services.code_indexer.clear_graph.assert_awaited_once_with("proj-1")


def test_rebuild_graph_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.post(
        "/api/code-index/graph/rebuild",
        params={"project_id": "proj-1", "limit": 50},
    )
    assert response.status_code == 200
    mock_server.services.code_indexer.rebuild_graph.assert_awaited_once_with(
        "proj-1",
        limit=50,
    )


def test_clear_graph_returns_500_on_exception(client: TestClient, mock_server: MagicMock) -> None:
    mock_server.services.code_indexer.clear_graph = AsyncMock(side_effect=RuntimeError("boom"))

    response = client.post("/api/code-index/graph/clear", params={"project_id": "proj-1"})

    assert response.status_code == 500
    assert response.json()["detail"] == "boom"


def test_rebuild_graph_returns_500_on_exception(client: TestClient, mock_server: MagicMock) -> None:
    mock_server.services.code_indexer.rebuild_graph = AsyncMock(side_effect=RuntimeError("boom"))

    response = client.post("/api/code-index/graph/rebuild", params={"project_id": "proj-1"})

    assert response.status_code == 500
    assert response.json()["detail"] == "boom"


def test_clear_graph_preserves_http_exception(
    client: TestClient, mock_server: MagicMock
) -> None:
    mock_server.services.code_indexer.clear_graph = AsyncMock(
        side_effect=HTTPException(status_code=418, detail="teapot")
    )

    response = client.post("/api/code-index/graph/clear", params={"project_id": "proj-1"})

    assert response.status_code == 418
    assert response.json()["detail"] == "teapot"


def test_rebuild_graph_preserves_http_exception(
    client: TestClient, mock_server: MagicMock
) -> None:
    mock_server.services.code_indexer.rebuild_graph = AsyncMock(
        side_effect=HTTPException(status_code=422, detail="bad rebuild request")
    )

    response = client.post("/api/code-index/graph/rebuild", params={"project_id": "proj-1"})

    assert response.status_code == 422
    assert response.json()["detail"] == "bad rebuild request"
