"""Tests for code-index HTTP routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from gobby.code_index.context import (
    CodeIndexContext,
    CodeIndexGraphUnavailable,
    CodeIndexProjectNotFound,
)
from gobby.code_index.gcode_gateway import (
    GcodeCommandError,
    GcodeInputValidationError,
    GcodeProjectNotFoundError,
    GcodeUnavailableError,
)
from gobby.code_index.models import IndexedProject, Symbol
from gobby.config.code_index import CodeIndexConfig
from gobby.config.wiki import WikiConfig
from gobby.servers.routes.code_index import create_code_index_router

pytestmark = pytest.mark.unit

# projects.id and code_* ids are native uuid columns; use a valid UUID string.
PROJECT_ID = "11111111-1111-4111-8111-111111111111"


def _make_symbol(symbol_id: str = "sym-1", name: str = "handler") -> Symbol:
    return Symbol(
        id=symbol_id,
        project_id=PROJECT_ID,
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


def _make_project_row(project_id: str = PROJECT_ID, name: str = "gobby") -> dict[str, Any]:
    return {
        "id": project_id,
        "name": name,
        "repo_path": "/repo",
        "github_url": None,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }


@pytest.fixture
def mock_server() -> MagicMock:
    server = MagicMock()
    server.config = SimpleNamespace(
        wiki=WikiConfig(codewiki_project_scopes_by_name={"gobby": ["crates", "web", "src"]})
    )
    server.services = MagicMock()
    server.services.database = MagicMock()
    server.services.database.fetchone.return_value = _make_project_row()
    code_indexer = MagicMock()
    code_indexer.graph_overview = AsyncMock(return_value={"nodes": [], "links": []})
    code_indexer.graph_file = AsyncMock(return_value={"nodes": [], "links": []})
    code_indexer.graph_symbol_neighbors = AsyncMock(return_value={"nodes": [], "links": []})
    code_indexer.graph_blast_radius = AsyncMock(
        return_value={"nodes": [], "links": [], "center": "sym-1"}
    )
    code_indexer.graph_path = AsyncMock(return_value={"path": [], "summary": {"depth": 2}})
    code_indexer.clear_graph = AsyncMock(return_value={"success": True, "project_id": PROJECT_ID})
    code_indexer.rebuild_graph = AsyncMock(
        return_value={"success": True, "project_id": PROJECT_ID, "files_processed": 0, "errors": []}
    )
    code_indexer.invalidate = AsyncMock(return_value={"status": "ok", "project_id": PROJECT_ID})
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
    response = client.get("/api/code-index/graph", params={"project_id": PROJECT_ID, "limit": 25})
    assert response.status_code == 200
    mock_server.services.code_indexer.graph_overview.assert_awaited_once_with(
        PROJECT_ID,
        limit=25,
    )


def test_codewiki_routes_absent(client: TestClient) -> None:
    response = client.post("/api/code-index/codewiki/refresh", json={"root_path": "/repo"})
    status = client.get("/api/code-index/codewiki/status")

    assert response.status_code == 404
    assert status.status_code == 404


def test_graph_file_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.get(
        "/api/code-index/graph/file/src/app.py",
        params={"project_id": PROJECT_ID},
    )
    assert response.status_code == 200
    mock_server.services.code_indexer.graph_file.assert_awaited_once_with(
        PROJECT_ID,
        "src/app.py",
    )


def test_graph_route_returns_400_for_invalid_gcode_input(
    client: TestClient,
    mock_server: MagicMock,
) -> None:
    mock_server.services.code_indexer.graph_file.side_effect = GcodeInputValidationError(
        "file_path",
        "-src/app.py",
        "value must not start with '-'",
    )

    response = client.get(
        "/api/code-index/graph/file/-src/app.py",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Invalid file_path: value must not start with '-'",
    }


def test_graph_symbol_neighbors_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.get(
        "/api/code-index/graph/symbol/sym-1/neighbors",
        params={"project_id": PROJECT_ID, "limit": 10},
    )
    assert response.status_code == 200
    mock_server.services.code_indexer.graph_symbol_neighbors.assert_awaited_once_with(
        PROJECT_ID,
        "sym-1",
        limit=10,
    )


def test_blast_radius_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.get(
        "/api/code-index/graph/blast-radius",
        params={"project_id": PROJECT_ID, "symbol_id": "sym-1", "depth": 2, "limit": 20},
    )
    assert response.status_code == 200
    mock_server.services.code_indexer.graph_blast_radius.assert_awaited_once_with(
        PROJECT_ID,
        symbol_id="sym-1",
        file_path=None,
        depth=2,
        limit=20,
    )


def test_graph_path_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.get(
        "/api/code-index/graph/path",
        params={
            "project_id": PROJECT_ID,
            "symbol_a": "from-symbol",
            "symbol_b": "to-symbol",
            "max_depth": 8,
        },
    )
    assert response.status_code == 200
    mock_server.services.code_indexer.graph_path.assert_awaited_once_with(
        PROJECT_ID,
        "from-symbol",
        "to-symbol",
        max_depth=8,
    )


def test_search_falls_back_to_name_search(client: TestClient, mock_server: MagicMock) -> None:
    response = client.get(
        "/api/code-index/graph/search", params={"project_id": PROJECT_ID, "q": "handler"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["id"] == "sym-1"
    assert data["results"][0]["type"] == "function"
    mock_server.services.code_indexer.storage.search_symbols_fts.assert_called_once_with(
        "handler",
        PROJECT_ID,
        kind=None,
        file_path=None,
        limit=25,
    )
    mock_server.services.code_indexer.storage.search_symbols_by_name.assert_called_once_with(
        "handler",
        PROJECT_ID,
        kind=None,
        file_path=None,
        limit=25,
    )


def test_blast_radius_validates_exclusive_target(client: TestClient) -> None:
    response = client.get(
        "/api/code-index/graph/blast-radius",
        params={"project_id": PROJECT_ID, "symbol_id": "sym-1", "file_path": "src/app.py"},
    )
    assert response.status_code == 400


def test_clear_graph_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.post("/api/code-index/graph/clear", params={"project_id": PROJECT_ID})
    assert response.status_code == 200
    mock_server.services.code_indexer.clear_graph.assert_awaited_once_with(PROJECT_ID)


def test_rebuild_graph_delegates(client: TestClient, mock_server: MagicMock) -> None:
    response = client.post(
        "/api/code-index/graph/rebuild",
        params={"project_id": PROJECT_ID},
    )
    assert response.status_code == 200
    mock_server.services.code_indexer.rebuild_graph.assert_awaited_once_with(PROJECT_ID)

    operation = cast(FastAPI, client.app).openapi()["paths"]["/api/code-index/graph/rebuild"][
        "post"
    ]
    assert [parameter["name"] for parameter in operation["parameters"]] == ["project_id"]


def test_agent_projection_brokers_reject_cross_project_targets(mock_server: MagicMock) -> None:
    mock_server.auth_service.verified_agent_claims.return_value = SimpleNamespace(
        project_id=PROJECT_ID
    )
    app = FastAPI()
    app.include_router(create_code_index_router(mock_server))
    client = TestClient(app, headers={"Authorization": "Bearer agent-token"})

    clear = client.post(
        "/api/code-index/graph/clear",
        params={"project_id": "other-project"},
    )
    invalidate = client.post(
        "/api/code-index/invalidate",
        json={"project_id": "other-project"},
    )

    assert clear.status_code == 403
    assert invalidate.status_code == 403
    mock_server.services.code_indexer.clear_graph.assert_not_awaited()
    mock_server.services.code_indexer.invalidate.assert_not_awaited()


def test_invalidate_keeps_shared_projections() -> None:
    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    storage = MagicMock()
    storage.get_project_stats.return_value = IndexedProject(
        id=PROJECT_ID,
        root_path="/repo",
        total_files=0,
        total_symbols=0,
    )
    storage.delete_project_index.return_value = {
        "symbols": 0,
        "files": 0,
        "imports": 0,
        "calls": 0,
        "content_chunks": 0,
        "projects": 1,
    }
    storage.clear_projection_cleanup_pending.return_value = False
    gcode_gateway = SimpleNamespace(
        vector_clear=AsyncMock(return_value={"success": False, "error": "down"})
    )
    code_indexer = CodeIndexContext(
        storage=storage,
        gcode_gateway=gcode_gateway,
        config=CodeIndexConfig(graph_enabled=False, embedding_enabled=True),
        run_db=run_db,
    )
    server = MagicMock()
    server.services = SimpleNamespace(code_indexer=code_indexer)
    server.run_db = run_db
    app = FastAPI()
    app.include_router(create_code_index_router(server))
    test_client = TestClient(app)

    response = test_client.post("/api/code-index/invalidate", json={"project_id": PROJECT_ID})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["stores"]["graph"] == {"status": "skipped"}
    assert body["stores"]["vector"] == {"status": "skipped"}
    assert body["failed_stores"] == []
    gcode_gateway.vector_clear.assert_not_awaited()
    storage.mark_prune_dirty.assert_not_called()
    storage.record_projection_cleanup_failure.assert_not_called()


def test_clear_graph_returns_500_on_exception(client: TestClient, mock_server: MagicMock) -> None:
    mock_server.services.code_indexer.clear_graph = AsyncMock(side_effect=RuntimeError("boom"))

    response = client.post("/api/code-index/graph/clear", params={"project_id": PROJECT_ID})

    assert response.status_code == 500
    assert response.json()["detail"] == "Code graph request failed"


def test_rebuild_graph_returns_500_on_exception(client: TestClient, mock_server: MagicMock) -> None:
    mock_server.services.code_indexer.rebuild_graph = AsyncMock(side_effect=RuntimeError("boom"))

    response = client.post("/api/code-index/graph/rebuild", params={"project_id": PROJECT_ID})

    assert response.status_code == 500
    assert response.json()["detail"] == "Code graph request failed"


def test_clear_graph_preserves_http_exception(client: TestClient, mock_server: MagicMock) -> None:
    mock_server.services.code_indexer.clear_graph = AsyncMock(
        side_effect=HTTPException(status_code=418, detail="teapot")
    )

    response = client.post("/api/code-index/graph/clear", params={"project_id": PROJECT_ID})

    assert response.status_code == 418
    assert response.json()["detail"] == "teapot"


def test_rebuild_graph_preserves_http_exception(client: TestClient, mock_server: MagicMock) -> None:
    mock_server.services.code_indexer.rebuild_graph = AsyncMock(
        side_effect=HTTPException(status_code=422, detail="bad rebuild request")
    )

    response = client.post("/api/code-index/graph/rebuild", params={"project_id": PROJECT_ID})

    assert response.status_code == 422
    assert response.json()["detail"] == "bad rebuild request"


def test_graph_route_returns_503_when_gateway_unavailable(
    client: TestClient,
    mock_server: MagicMock,
) -> None:
    mock_server.services.code_indexer.graph_overview = AsyncMock(
        side_effect=GcodeUnavailableError("gcode is not installed")
    )

    response = client.get("/api/code-index/graph", params={"project_id": PROJECT_ID})

    assert response.status_code == 503
    assert response.json()["detail"] == "Code graph not available"


def test_graph_route_returns_503_when_graph_disabled(
    client: TestClient,
    mock_server: MagicMock,
) -> None:
    mock_server.services.code_indexer.graph_file = AsyncMock(
        side_effect=CodeIndexGraphUnavailable("Code graph not available")
    )

    response = client.get(
        "/api/code-index/graph/file/src/app.py",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Code graph not available"


def test_graph_route_returns_404_when_project_root_missing(
    client: TestClient,
    mock_server: MagicMock,
) -> None:
    mock_server.services.code_indexer.graph_symbol_neighbors = AsyncMock(
        side_effect=CodeIndexProjectNotFound("Code index project not found: proj-1")
    )

    response = client.get(
        "/api/code-index/graph/symbol/sym-1/neighbors",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Code index project not found: proj-1"


def test_graph_route_returns_404_when_gcode_project_missing(
    client: TestClient,
    mock_server: MagicMock,
) -> None:
    error = GcodeProjectNotFoundError(
        ["gcode", "graph", "overview"],
        2,
        "project not found for root /stale/project",
        "/stale/project",
    )
    mock_server.services.code_indexer.graph_overview = AsyncMock(side_effect=error)

    response = client.get("/api/code-index/graph", params={"project_id": PROJECT_ID})

    assert response.status_code == 404
    assert response.json()["detail"] == str(error)


def test_graph_route_returns_500_when_gcode_command_fails(
    client: TestClient,
    mock_server: MagicMock,
) -> None:
    mock_server.services.code_indexer.graph_blast_radius = AsyncMock(
        side_effect=GcodeCommandError(["gcode", "graph", "blast-radius"], 2, "boom")
    )

    response = client.get(
        "/api/code-index/graph/blast-radius",
        params={"project_id": PROJECT_ID, "file_path": "src/app.py"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Code graph request failed"
