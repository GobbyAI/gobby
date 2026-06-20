"""Tests for memory HTTP REST routes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.persistence import MemoryDreamConfig
from gobby.servers.routes.memory import create_memory_router
from gobby.servers.routes.memory_dream import create_memory_dream_router
from gobby.storage.memories import Memory

pytestmark = pytest.mark.unit

NOW_ISO = "2026-02-10T12:00:00+00:00"


def _make_memory(**overrides) -> Memory:
    """Create a Memory with defaults."""
    defaults = {
        "id": "mm-abc123",
        "memory_type": "fact",
        "content": "User prefers dark mode",
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
        "project_id": "test-project",
        "source_type": "user",
        "source_session_id": None,
        "access_count": 3,
        "last_accessed_at": NOW_ISO,
        "tags": ["ui", "preference"],
    }
    defaults.update(overrides)
    return Memory(**defaults)


@pytest.fixture
def mock_server():
    """Create mock HTTPServer with memory_manager."""
    server = MagicMock()
    server._background_tasks = set()
    server.register_background_task.side_effect = lambda task: server._background_tasks.add(task)
    server.services = SimpleNamespace(
        project_id="test-project",
        config=SimpleNamespace(memory=SimpleNamespace(dream=MemoryDreamConfig())),
    )
    server.llm_service = None
    server.memory_manager = MagicMock()
    server.memory_manager.create_memory = AsyncMock(return_value=_make_memory())
    server.memory_manager.search_memories = AsyncMock(return_value=[])
    server.memory_manager.update_memory = AsyncMock(return_value=_make_memory())
    server.memory_manager.delete_memory = AsyncMock(return_value=True)
    server.memory_manager.count_memories.return_value = 0
    return server


@pytest.fixture
def client(mock_server):
    """Create TestClient with memory router."""
    with patch(
        "gobby.utils.project_context.get_project_context", return_value={"id": "test-project"}
    ):
        app = FastAPI()
        router = create_memory_router(mock_server)
        app.include_router(router)
        yield TestClient(app)


@pytest.fixture
def dream_client(mock_server) -> TestClient:
    """Create TestClient with memory dream router."""
    app = FastAPI()
    app.include_router(create_memory_dream_router(mock_server))
    return TestClient(app)


class TestMemoryDreamRoutes:
    """Test memory dream HTTP endpoints."""

    def test_start_dream_returns_run_id(
        self, dream_client: TestClient, mock_server: MagicMock
    ) -> None:
        with patch("gobby.servers.routes.memory_dream.MemoryDreamService") as service_cls:
            service = service_cls.return_value
            service.start_async = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
            service.execute_run = AsyncMock(return_value={"success": True})

            response = dream_client.post(
                "/memory/dream",
                json={"dry_run": True, "project_id": "proj-1", "memory_type": "fact"},
            )

        assert response.status_code == 202
        assert response.json()["run_id"] == "dream-1"
        service.start_async.assert_awaited_once()
        options = service.start_async.await_args.args[0]
        assert options.dry_run is True
        assert options.project_id == "proj-1"
        assert options.memory_type == "fact"
        mock_server.register_background_task.assert_called_once()

    def test_wait_dream_returns_completed_run(self, dream_client: TestClient) -> None:
        with patch("gobby.servers.routes.memory_dream.MemoryDreamService") as service_cls:
            service = service_cls.return_value
            service.run = AsyncMock(
                return_value={"success": True, "run_id": "dream-1", "run": {"status": "completed"}}
            )

            response = dream_client.post(
                "/memory/dream",
                json={"wait": True, "skip_consolidation": True},
            )

        assert response.status_code == 200
        assert response.json()["run"]["status"] == "completed"
        service.run.assert_awaited_once()
        assert service.run.await_args.args[0].skip_consolidation is True

    def test_status_and_revert(self, dream_client: TestClient) -> None:
        with patch("gobby.servers.routes.memory_dream.MemoryDreamService") as service_cls:
            service = service_cls.return_value
            service.status = AsyncMock(return_value={"success": True, "run": {"id": "dream-1"}})
            service.revert = AsyncMock(return_value={"success": True, "run_id": "dream-1"})

            status = dream_client.get("/memory/dream/dream-1")
            revert = dream_client.post("/memory/dream/dream-1/revert")

        assert status.status_code == 200
        assert revert.status_code == 200
        service.status.assert_awaited_once_with("dream-1")
        service.revert.assert_awaited_once_with("dream-1")

    def test_missing_memory_manager_returns_503(self, mock_server: MagicMock) -> None:
        mock_server.memory_manager = None
        app = FastAPI()
        app.include_router(create_memory_dream_router(mock_server))

        response = TestClient(app).post("/memory/dream", json={"wait": True})

        assert response.status_code == 503
        assert response.json()["detail"] == "memory manager is unavailable"

    def test_missing_dream_config_returns_503(self, mock_server: MagicMock) -> None:
        mock_server.services.config.memory.dream = None
        app = FastAPI()
        app.include_router(create_memory_dream_router(mock_server))

        response = TestClient(app).post("/memory/dream", json={"wait": True})

        assert response.status_code == 503
        assert response.json()["detail"] == "memory dream config is unavailable"


# =============================================================================
# GET /memories - list
# =============================================================================


class TestListMemories:
    """Test GET /memories endpoint."""

    def test_list_returns_memories(self, client, mock_server) -> None:
        """GET /memories returns a list of memories."""
        mock_server.memory_manager.list_memories.return_value = [
            _make_memory(id="mm-1", content="Memory one"),
            _make_memory(id="mm-2", content="Memory two"),
        ]
        response = client.get("/api/memories")
        assert response.status_code == 200
        data = response.json()
        assert len(data["memories"]) == 2
        assert data["memories"][0]["id"] == "mm-1"

    def test_list_with_filters(self, client, mock_server) -> None:
        """GET /memories supports query parameter filters."""
        mock_server.memory_manager.list_memories.return_value = []
        response = client.get(
            "/api/memories",
            params={
                "project_id": "proj-1",
                "memory_type": "fact",
                "limit": 20,
            },
        )
        assert response.status_code == 200
        mock_server.memory_manager.list_memories.assert_called_once_with(
            project_id="proj-1",
            memory_type="fact",
            limit=20,
            offset=0,
            visibility="active",
        )
        mock_server.memory_manager.count_memories.assert_called_once_with(
            project_id="proj-1",
            memory_type="fact",
            visibility="active",
        )

    def test_list_empty(self, client, mock_server) -> None:
        """GET /memories returns empty list when no memories."""
        mock_server.memory_manager.list_memories.return_value = []
        response = client.get("/api/memories")
        assert response.status_code == 200
        assert response.json()["memories"] == []

    def test_list_defaults_to_active_visibility(self, client, mock_server) -> None:
        """Both the page and its total default to active visibility."""
        mock_server.memory_manager.list_memories.return_value = []
        mock_server.memory_manager.count_memories.return_value = 0
        response = client.get("/api/memories")
        assert response.status_code == 200
        mock_server.memory_manager.list_memories.assert_called_once_with(
            project_id=None,
            memory_type=None,
            limit=50,
            offset=0,
            visibility="active",
        )
        mock_server.memory_manager.count_memories.assert_called_once_with(
            project_id=None,
            memory_type=None,
            visibility="active",
        )

    @pytest.mark.parametrize("visibility", ["active", "hidden", "all"])
    def test_list_visibility_passthrough(self, client, mock_server, visibility) -> None:
        """visibility is threaded to the page and shared with the total."""
        mock_server.memory_manager.list_memories.return_value = []
        mock_server.memory_manager.count_memories.return_value = 7
        response = client.get("/api/memories", params={"visibility": visibility})
        assert response.status_code == 200
        assert response.json()["total_memories"] == 7
        assert mock_server.memory_manager.list_memories.call_args.kwargs["visibility"] == visibility
        mock_server.memory_manager.count_memories.assert_called_once_with(
            project_id=None,
            memory_type=None,
            visibility=visibility,
        )

    def test_list_rejects_invalid_visibility(self, client, mock_server) -> None:
        """An out-of-enum visibility value fails FastAPI validation with 422."""
        response = client.get("/api/memories", params={"visibility": "bogus"})
        assert response.status_code == 422
        mock_server.memory_manager.list_memories.assert_not_called()


# =============================================================================
# POST /memories - create
# =============================================================================


class TestCreateMemory:
    """Test POST /memories endpoint."""

    def test_create_memory(self, client, mock_server) -> None:
        """POST /memories creates a memory and returns id."""
        mock_server.memory_manager.create_memory = AsyncMock(
            return_value=_make_memory(id="mm-new-123")
        )
        response = client.post(
            "/api/memories",
            json={
                "content": "User prefers dark mode",
                "memory_type": "preference",
                "project_id": "test-project",
                "tags": ["ui"],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "mm-new-123"
        assert data["content"] == "User prefers dark mode"

    def test_create_requires_content(self, client, mock_server) -> None:
        """POST /memories requires content field."""
        response = client.post("/api/memories", json={})
        assert response.status_code == 422

    def test_create_memory_server_error(self, client, mock_server) -> None:
        """POST /memories returns 500 when manager raises error."""
        mock_server.memory_manager.create_memory.side_effect = RuntimeError("Backend failure")
        response = client.post(
            "/api/memories",
            json={"content": "test"},
        )
        assert response.status_code == 500
        assert "Backend failure" in response.json()["detail"]


# =============================================================================
# GET /memories/{id} - detail
# =============================================================================


class TestGetMemory:
    """Test GET /memories/{id} endpoint."""

    def test_get_memory(self, client, mock_server) -> None:
        """GET /memories/{id} returns memory detail."""
        mock_server.memory_manager.get_memory.return_value = _make_memory()
        response = client.get("/api/memories/mm-abc123")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "mm-abc123"
        assert data["content"] == "User prefers dark mode"
        assert data["tags"] == ["ui", "preference"]

    def test_get_memory_not_found(self, client, mock_server) -> None:
        """GET /memories/{id} returns 404 when not found."""
        mock_server.memory_manager.get_memory.return_value = None
        response = client.get("/api/memories/nonexistent")
        assert response.status_code == 404


# =============================================================================
# PUT /memories/{id} - update
# =============================================================================


class TestUpdateMemory:
    """Test PUT /memories/{id} endpoint."""

    def test_update_memory(self, client, mock_server) -> None:
        """PUT /memories/{id} updates and returns memory."""
        mock_server.memory_manager.update_memory.return_value = _make_memory(
            content="Updated content"
        )
        response = client.put(
            "/api/memories/mm-abc123",
            json={"content": "Updated content"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["content"] == "Updated content"

    def test_update_not_found(self, client, mock_server) -> None:
        """PUT /memories/{id} returns 404 when not found."""
        mock_server.memory_manager.update_memory.side_effect = ValueError("Memory not found")
        response = client.put("/api/memories/nonexistent", json={"content": "new content"})
        assert response.status_code == 404


# =============================================================================
# DELETE /memories/{id} - delete
# =============================================================================


class TestDeleteMemory:
    """Test DELETE /memories/{id} endpoint."""

    def test_delete_memory(self, client, mock_server) -> None:
        """DELETE /memories/{id} removes memory."""
        mock_server.memory_manager.delete_memory.return_value = True
        response = client.delete("/api/memories/mm-abc123")
        assert response.status_code == 200
        assert response.json()["deleted"] is True
        mock_server.memory_manager.delete_memory.assert_called_once_with("mm-abc123")

    def test_delete_not_found(self, client, mock_server) -> None:
        """DELETE /memories/{id} returns 404 when not found."""
        mock_server.memory_manager.delete_memory.return_value = False
        response = client.delete("/api/memories/nonexistent")
        assert response.status_code == 404


class TestRestoreMemory:
    """Test POST /memories/{id}/restore endpoint."""

    def test_restore_returns_restored_memory(self, client, mock_server) -> None:
        """POST /memories/{id}/restore un-hides the row and returns it."""
        mock_server.memory_manager.restore_memory.return_value = True
        mock_server.memory_manager.get_memory.return_value = _make_memory(id="mm-restored")
        response = client.post("/api/memories/mm-restored/restore")
        assert response.status_code == 200
        assert response.json()["id"] == "mm-restored"
        mock_server.memory_manager.restore_memory.assert_called_once_with("mm-restored")

    def test_restore_not_found(self, client, mock_server) -> None:
        """A missing memory raises ValueError in storage and surfaces as 404."""
        mock_server.memory_manager.get_memory.return_value = None
        response = client.post("/api/memories/nope/restore")
        assert response.status_code == 404
        mock_server.memory_manager.restore_memory.assert_not_called()

    def test_restore_server_error(self, client, mock_server) -> None:
        """An unexpected storage failure surfaces as 500."""
        mock_server.memory_manager.get_memory.return_value = _make_memory(id="mm-1")
        mock_server.memory_manager.restore_memory.side_effect = RuntimeError("DB error")
        response = client.post("/api/memories/mm-1/restore")
        assert response.status_code == 500


class TestPromoteMemory:
    """Test POST /memories/{id}/promote endpoint."""

    def test_promote_returns_global_memory(self, client, mock_server) -> None:
        """POST /memories/{id}/promote moves a row to global scope and returns it."""
        mock_server.memory_manager.get_memory.return_value = _make_memory(id="mm-promoted")
        promoted = _make_memory(id="mm-promoted", project_id=None)
        mock_server.memory_manager.rescope_memory = AsyncMock(return_value=promoted)

        response = client.post("/api/memories/mm-promoted/promote")

        assert response.status_code == 200
        assert response.json()["id"] == "mm-promoted"
        assert response.json()["project_id"] is None
        mock_server.memory_manager.rescope_memory.assert_awaited_once_with(
            "mm-promoted",
            None,
        )

    def test_promote_rejects_non_global_target(self, client, mock_server) -> None:
        """Only promote-to-global is exposed."""
        mock_server.memory_manager.rescope_memory = AsyncMock()

        response = client.post(
            "/api/memories/mm-promoted/promote",
            json={"target_project_id": "other-project"},
        )

        assert response.status_code == 422
        mock_server.memory_manager.rescope_memory.assert_not_called()

    def test_promote_not_found(self, client, mock_server) -> None:
        """A missing memory raises ValueError in storage and surfaces as 404."""
        mock_server.memory_manager.get_memory.return_value = _make_memory(id="nope")
        mock_server.memory_manager.rescope_memory = AsyncMock(
            side_effect=ValueError("Memory nope not found")
        )

        response = client.post("/api/memories/nope/promote")

        assert response.status_code == 404


# =============================================================================
# GET /memories/search - search
# =============================================================================


class TestSearchMemories:
    """Test GET /memories/search endpoint."""

    def test_search_returns_results(self, client, mock_server) -> None:
        """GET /memories/search?q=query returns ranked results."""
        mock_server.memory_manager.search_memories.return_value = [
            _make_memory(id="mm-1", content="Dark mode preference"),
        ]
        response = client.get("/api/memories/search", params={"q": "dark mode"})
        assert response.status_code == 200
        data = response.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["id"] == "mm-1"
        assert data["query"] == "dark mode"

    def test_search_requires_query(self, client, mock_server) -> None:
        """GET /memories/search requires q parameter."""
        response = client.get("/api/memories/search")
        assert response.status_code == 422

    def test_search_with_filters(self, client, mock_server) -> None:
        """GET /memories/search supports project_id and limit filters."""
        mock_server.memory_manager.search_memories.return_value = []
        response = client.get(
            "/api/memories/search",
            params={"q": "test", "project_id": "proj-1", "limit": 5},
        )
        assert response.status_code == 200
        mock_server.memory_manager.search_memories.assert_called_once_with(
            query="test",
            project_id="proj-1",
            limit=5,
            caller="http.memory.search",
        )


# =============================================================================
# GET /memories/stats - statistics
# =============================================================================


class TestMemoryStats:
    """Test GET /memories/stats endpoint."""

    def test_stats_returns_counts(self, client, mock_server) -> None:
        """GET /memories/stats returns memory statistics."""
        mock_server.memory_manager.get_stats.return_value = {
            "total_count": 42,
            "by_type": {"fact": 30, "preference": 12},
            "project_id": None,
        }
        response = client.get("/api/memories/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 42
        assert data["by_type"]["fact"] == 30

    def test_stats_with_project_filter(self, client, mock_server) -> None:
        """GET /memories/stats supports project_id filter."""
        mock_server.memory_manager.get_stats.return_value = {
            "total_count": 10,
            "by_type": {"fact": 10},
            "project_id": "proj-1",
        }
        response = client.get("/api/memories/stats", params={"project_id": "proj-1"})
        assert response.status_code == 200
        mock_server.memory_manager.get_stats.assert_called_once_with(project_id="proj-1")

    def test_stats_server_error(self, client, mock_server) -> None:
        """GET /memories/stats returns 500 on error."""
        mock_server.memory_manager.get_stats.side_effect = RuntimeError("DB error")
        response = client.get("/api/memories/stats")
        assert response.status_code == 500


# =============================================================================
# GET /memories/graph - memory graph
# =============================================================================


class TestMemoryGraph:
    """Test GET /memories/graph endpoint."""

    def test_graph_returns_data(self, client, mock_server) -> None:
        """GET /memories/graph returns memories and crossrefs."""
        mock_server.memory_manager.list_memories.return_value = [
            _make_memory(id="mm-1"),
            _make_memory(id="mm-2"),
        ]
        mock_crossref = MagicMock()
        mock_crossref.source_id = "mm-1"
        mock_crossref.target_id = "mm-2"
        mock_crossref.to_dict.return_value = {
            "source_id": "mm-1",
            "target_id": "mm-2",
            "similarity": 0.9,
        }
        mock_server.memory_manager.storage.get_all_crossrefs.return_value = [mock_crossref]
        response = client.get("/api/memories/graph")
        assert response.status_code == 200
        data = response.json()
        assert "memories" in data
        assert "crossrefs" in data
        assert len(data["memories"]) == 2
        assert len(data["crossrefs"]) == 1

    def test_graph_filters_by_project(self, client, mock_server) -> None:
        """GET /memories/graph respects project_id filter."""
        mock_server.memory_manager.list_memories.return_value = []
        mock_server.memory_manager.storage.get_all_crossrefs.return_value = []
        response = client.get("/api/memories/graph", params={"project_id": "proj-1"})
        assert response.status_code == 200
        mock_server.memory_manager.list_memories.assert_called_once_with(
            project_id="proj-1", limit=200
        )

    def test_graph_server_error(self, client, mock_server) -> None:
        """GET /memories/graph returns 500 on error."""
        mock_server.memory_manager.list_memories.side_effect = RuntimeError("DB error")
        response = client.get("/api/memories/graph")
        assert response.status_code == 500

    def test_graph_filters_crossrefs_by_visible_memories(self, client, mock_server) -> None:
        """GET /memories/graph only includes crossrefs for visible memories."""
        mock_server.memory_manager.list_memories.return_value = [_make_memory(id="mm-1")]
        # Crossref references an invisible memory
        mock_crossref = MagicMock()
        mock_crossref.source_id = "mm-1"
        mock_crossref.target_id = "mm-invisible"
        mock_server.memory_manager.storage.get_all_crossrefs.return_value = [mock_crossref]
        response = client.get("/api/memories/graph")
        assert response.status_code == 200
        assert len(response.json()["crossrefs"]) == 0


# =============================================================================
# POST /memories/crossrefs/rebuild
# =============================================================================


class TestRebuildCrossrefs:
    """Test POST /memories/crossrefs/rebuild endpoint."""

    def test_rebuild_crossrefs(self, client, mock_server) -> None:
        """POST /memories/crossrefs/rebuild processes memories."""
        mock_server.memory_manager.list_memories.return_value = [
            _make_memory(id="mm-1"),
            _make_memory(id="mm-2"),
        ]
        mock_server.memory_manager.rebuild_crossrefs_for_memory = AsyncMock(return_value=1)
        response = client.post("/api/memories/crossrefs/rebuild")
        assert response.status_code == 200
        data = response.json()
        assert data["memories_processed"] == 2
        assert data["crossrefs_created"] == 2

    def test_rebuild_crossrefs_partial_failure(self, client, mock_server) -> None:
        """POST /memories/crossrefs/rebuild handles per-memory failures."""
        mock_server.memory_manager.list_memories.return_value = [
            _make_memory(id="mm-1"),
            _make_memory(id="mm-2"),
        ]
        mock_server.memory_manager.rebuild_crossrefs_for_memory = AsyncMock(
            side_effect=[RuntimeError("fail"), 1]
        )
        response = client.post("/api/memories/crossrefs/rebuild")
        assert response.status_code == 200
        data = response.json()
        assert data["memories_processed"] == 2
        assert data["crossrefs_created"] == 1

    def test_rebuild_crossrefs_server_error(self, client, mock_server) -> None:
        """POST /memories/crossrefs/rebuild returns 500 on total failure."""
        mock_server.memory_manager.list_memories.side_effect = RuntimeError("DB error")
        response = client.post("/api/memories/crossrefs/rebuild")
        assert response.status_code == 500


# =============================================================================
# POST /memories/embeddings/reindex
# =============================================================================


class TestReindexEmbeddings:
    """Test POST /memories/embeddings/reindex endpoint."""

    def test_reindex(self, client, mock_server) -> None:
        """POST /memories/embeddings/reindex returns result."""
        mock_server.memory_manager.reindex_embeddings = AsyncMock(
            return_value={"success": True, "total_memories": 5, "embeddings_generated": 5}
        )
        response = client.post("/api/memories/embeddings/reindex")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["total_memories"] == 5

    def test_reindex_server_error(self, client, mock_server) -> None:
        """POST /memories/embeddings/reindex returns 500 on error."""
        mock_server.memory_manager.reindex_embeddings = AsyncMock(
            side_effect=RuntimeError("No vectorstore")
        )
        response = client.post("/api/memories/embeddings/reindex")
        assert response.status_code == 500


# =============================================================================
# GET /memories/graph/entities - entity graph
# =============================================================================


class TestEntityGraph:
    """Test GET /memories/graph/entities endpoint."""

    def test_entity_graph_no_falkordb(self, client, mock_server) -> None:
        """GET /memories/graph/entities returns 404 when no FalkorDB."""
        mock_server.memory_manager._falkor_client = None
        response = client.get("/api/memories/graph/entities")
        assert response.status_code == 404
        assert response.json()["detail"] == "FalkorDB not configured"

    def test_entity_graph_success(self, client, mock_server) -> None:
        """GET /memories/graph/entities returns graph data."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_graph = AsyncMock(
            return_value={"entities": [], "relationships": []}
        )
        response = client.get("/api/memories/graph/entities")
        assert response.status_code == 200
        assert "entities" in response.json()

    def test_entity_graph_unreachable(self, client, mock_server) -> None:
        """GET /memories/graph/entities returns 502 when FalkorDB is unreachable."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_graph = AsyncMock(return_value=None)
        response = client.get("/api/memories/graph/entities")
        assert response.status_code == 502
        assert response.json()["detail"] == "FalkorDB unreachable"

    def test_entity_graph_server_error(self, client, mock_server) -> None:
        """GET /memories/graph/entities returns 500 on error."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_graph = AsyncMock(
            side_effect=RuntimeError("FalkorDB error")
        )
        response = client.get("/api/memories/graph/entities")
        assert response.status_code == 500

    def test_entity_graph_none_manager(self, client, mock_server) -> None:
        """GET /memories/graph/entities returns 404 when memory_manager is None."""
        mock_server.memory_manager = None
        response = client.get("/api/memories/graph/entities")
        assert response.status_code == 404


# =============================================================================
# GET /memories/graph/entities/{entity_key}/neighbors
# =============================================================================


class TestEntityNeighbors:
    """Test GET /memories/graph/entities/{entity_key}/neighbors endpoint."""

    def test_neighbors_no_falkordb(self, client, mock_server) -> None:
        """GET /memories/graph/entities/{entity_key}/neighbors returns 404 without FalkorDB."""
        mock_server.memory_manager._falkor_client = None
        response = client.get("/api/memories/graph/entities/test-entity/neighbors")
        assert response.status_code == 404
        assert response.json()["detail"] == "FalkorDB not configured"

    def test_neighbors_success(self, client, mock_server) -> None:
        """GET /memories/graph/entities/{entity_key}/neighbors returns neighbors."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_neighbors = AsyncMock(
            return_value={"entities": [], "relationships": []}
        )
        response = client.get("/api/memories/graph/entities/test-entity/neighbors")
        assert response.status_code == 200
        assert "entities" in response.json()

    def test_neighbors_unreachable(self, client, mock_server) -> None:
        """GET /memories/graph/entities/{entity_key}/neighbors returns 502 when unreachable."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_neighbors = AsyncMock(return_value=None)
        response = client.get("/api/memories/graph/entities/test-entity/neighbors")
        assert response.status_code == 502
        assert response.json()["detail"] == "FalkorDB unreachable"

    def test_neighbors_server_error(self, client, mock_server) -> None:
        """GET /memories/graph/entities/{entity_key}/neighbors returns 500 on error."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_neighbors = AsyncMock(
            side_effect=RuntimeError("FalkorDB error")
        )
        response = client.get("/api/memories/graph/entities/test-entity/neighbors")
        assert response.status_code == 500


class TestGraphCounts:
    """Test GET /memories/graph/counts endpoint."""

    def test_counts_success(self, client, mock_server) -> None:
        """GET /memories/graph/counts returns actual FalkorDB counts."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_knowledge_graph_counts = AsyncMock(
            return_value={
                "graph": "gobby_kg",
                "project_id": "proj-1",
                "memory_nodes": 3,
                "entity_nodes": 7,
                "relationships": 9,
            }
        )

        response = client.get("/api/memories/graph/counts", params={"project_id": "proj-1"})

        assert response.status_code == 200
        data = response.json()
        assert data["graph"] == "gobby_kg"
        assert data["memory_nodes"] == 3
        mock_server.memory_manager.get_knowledge_graph_counts.assert_awaited_once_with(
            project_id="proj-1"
        )

    def test_counts_requires_falkordb(self, client, mock_server) -> None:
        """GET /memories/graph/counts returns 404 when FalkorDB is not configured."""
        mock_server.memory_manager._falkor_client = None

        response = client.get("/api/memories/graph/counts")

        assert response.status_code == 404
        assert response.json()["detail"] == "FalkorDB not configured"


# =============================================================================
# POST /memories/graph/rebuild - knowledge graph rebuild
# =============================================================================


class TestRebuildKnowledgeGraph:
    """Test POST /memories/graph/rebuild endpoint."""

    def test_rebuild_returns_error_payload(self, client, mock_server) -> None:
        """POST /memories/graph/rebuild returns 400 when manager reports failure."""
        mock_server.memory_manager.rebuild_knowledge_graph = AsyncMock(
            return_value={"success": False, "error": "FalkorDB not configured"}
        )
        response = client.post("/api/memories/graph/rebuild")
        assert response.status_code == 400

    def test_rebuild_success(self, client, mock_server) -> None:
        """POST /memories/graph/rebuild processes memories."""
        mock_server.memory_manager.rebuild_knowledge_graph = AsyncMock(
            return_value={
                "success": True,
                "memories_processed": 1,
                "status_counts": {"success": 1},
                "memories_extracted": 1,
                "noop_no_entities": 0,
                "errors": [],
            }
        )
        response = client.post("/api/memories/graph/rebuild")
        assert response.status_code == 200
        data = response.json()
        assert data["memories_processed"] == 1
        assert data["memories_extracted"] == 1
        assert data["errors"] == []

    def test_rebuild_partial_error(self, client, mock_server) -> None:
        """POST /memories/graph/rebuild handles per-memory errors."""
        mock_server.memory_manager.rebuild_knowledge_graph = AsyncMock(
            return_value={
                "success": True,
                "memories_processed": 2,
                "status_counts": {"success": 1, "partial_failure": 1},
                "memories_extracted": 1,
                "noop_no_entities": 0,
                "errors": ["mm-2:fail"],
            }
        )
        response = client.post("/api/memories/graph/rebuild")
        assert response.status_code == 200
        data = response.json()
        assert data["memories_extracted"] == 1
        assert data["errors"] == ["mm-2:fail"]

    def test_rebuild_server_error(self, client, mock_server) -> None:
        """POST /memories/graph/rebuild returns 500 on total failure."""
        mock_server.memory_manager.rebuild_knowledge_graph = AsyncMock(
            side_effect=RuntimeError("DB error")
        )
        response = client.post("/api/memories/graph/rebuild")
        assert response.status_code == 500

    def test_rebuild_background_starts_job(self, client, mock_server, monkeypatch) -> None:
        """POST /memories/graph/rebuild?background=true starts a tracked background job."""
        fake_task = MagicMock()
        monkeypatch.setattr(
            "gobby.servers.routes.memory.asyncio.create_task",
            lambda coro, name=None: fake_task,
        )

        response = client.post("/api/memories/graph/rebuild", params={"background": "true"})

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "running"
        assert data["started"] is True
        assert data["job_id"]
        assert fake_task in mock_server._background_tasks

    def test_rebuild_background_status_reports_latest_job(
        self, client, mock_server, monkeypatch
    ) -> None:
        """GET /memories/graph/rebuild/status returns the current background job state."""
        fake_task = MagicMock()
        monkeypatch.setattr(
            "gobby.servers.routes.memory.asyncio.create_task",
            lambda coro, name=None: fake_task,
        )

        started = client.post("/api/memories/graph/rebuild", params={"background": "true"})
        job_id = started.json()["job_id"]
        response = client.get("/api/memories/graph/rebuild/status", params={"job_id": job_id})

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == job_id
        assert data["status"] == "running"

    def test_rebuild_background_reuses_running_job(self, client, mock_server, monkeypatch) -> None:
        """Second background rebuild request should attach to the active job."""
        fake_task = MagicMock()
        monkeypatch.setattr(
            "gobby.servers.routes.memory.asyncio.create_task",
            lambda coro, name=None: fake_task,
        )

        first = client.post("/api/memories/graph/rebuild", params={"background": "true"})
        second = client.post("/api/memories/graph/rebuild", params={"background": "true"})

        assert first.status_code == 202
        assert second.status_code == 202
        data = second.json()
        assert data["already_running"] is True
        assert data["started"] is False


# =============================================================================
# Error path tests
# =============================================================================


class TestErrorPaths:
    """Test error handling across endpoints."""

    def test_list_server_error(self, client, mock_server) -> None:
        """GET /memories returns 500 on error."""
        mock_server.memory_manager.list_memories.side_effect = RuntimeError("DB error")
        response = client.get("/api/memories")
        assert response.status_code == 500

    def test_get_memory_server_error(self, client, mock_server) -> None:
        """GET /memories/{id} returns 500 on error."""
        mock_server.memory_manager.get_memory.side_effect = RuntimeError("DB error")
        response = client.get("/api/memories/mm-abc123")
        assert response.status_code == 500

    def test_update_server_error(self, client, mock_server) -> None:
        """PUT /memories/{id} returns 500 on generic error."""
        mock_server.memory_manager.update_memory = AsyncMock(side_effect=RuntimeError("DB error"))
        response = client.put("/api/memories/mm-abc123", json={"content": "new"})
        assert response.status_code == 500

    def test_delete_server_error(self, client, mock_server) -> None:
        """DELETE /memories/{id} returns 500 on error."""
        mock_server.memory_manager.delete_memory = AsyncMock(side_effect=RuntimeError("DB error"))
        response = client.delete("/api/memories/mm-abc123")
        assert response.status_code == 500

    def test_search_server_error(self, client, mock_server) -> None:
        """GET /memories/search returns 500 on error."""
        mock_server.memory_manager.search_memories = AsyncMock(
            side_effect=RuntimeError("Search error")
        )
        response = client.get("/api/memories/search", params={"q": "test"})
        assert response.status_code == 500
