"""Tests for memory HTTP REST routes."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.persistence import MemoryDreamConfig
from gobby.servers.routes.memory import create_memory_router
from gobby.servers.routes.memory_dream import create_memory_dream_router
from gobby.storage.memories import Memory
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope

pytestmark = pytest.mark.unit

NOW_ISO = "2026-02-10T12:00:00+00:00"
_VALID_RATIONALE = "Future sessions should match the user's established preference."


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
    server.memory_manager.restore_memory_indices = AsyncMock(return_value=True)
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
def dream_coordinator(mock_server) -> MagicMock:
    """Attach a fake daemon-owned dream coordinator to the mock server."""
    coordinator = MagicMock()
    coordinator.trigger = AsyncMock()
    coordinator.trigger_all_due_projects = AsyncMock()
    coordinator.service.status = AsyncMock()
    coordinator.service.revert = AsyncMock()
    mock_server.services.memory_dream_coordinator = coordinator
    return coordinator


@pytest.fixture
def dream_client(mock_server, dream_coordinator) -> TestClient:
    """Create TestClient with memory dream router."""
    app = FastAPI()
    app.include_router(create_memory_dream_router(mock_server))
    return TestClient(app)


class TestMemoryDreamRoutes:
    """Test memory dream HTTP endpoints."""

    def test_start_dream_returns_run_id_immediately(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        dream_coordinator.trigger.return_value = {
            "success": True,
            "run_id": "dream-1",
            "status": "running",
            "coalesced": False,
        }

        # A legacy "wait" key is ignored: the route exposes no wait parameter
        # and every trigger is asynchronous.
        response = dream_client.post(
            "/memory/dream",
            json={"dry_run": True, "project_id": "proj-1", "memory_type": "fact", "wait": True},
        )

        assert response.status_code == 202
        assert response.json() == {
            "success": True,
            "run_id": "dream-1",
            "status": "running",
            "coalesced": False,
        }
        dream_coordinator.trigger.assert_awaited_once()
        options = dream_coordinator.trigger.await_args.args[0]
        assert options.dry_run is True
        assert options.project_id == "proj-1"
        assert options.memory_type == "fact"
        dream_coordinator.trigger_all_due_projects.assert_not_awaited()

    def test_unscoped_dream_triggers_all_due_projects(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        dream_coordinator.trigger_all_due_projects.return_value = {
            "success": True,
            "run_id": "aggregate-1",
            "status": "running",
            "coalesced": False,
        }

        response = dream_client.post("/memory/dream", json={"dry_run": True, "full_sweep": True})

        assert response.status_code == 202
        assert response.json()["run_id"] == "aggregate-1"
        dream_coordinator.trigger_all_due_projects.assert_awaited_once_with(
            dry_run=True,
            skip_consolidation=False,
            memory_type=None,
            full_sweep=True,
        )
        # An unscoped trigger never runs the single-digest path.
        dream_coordinator.trigger.assert_not_awaited()

    def test_coalesced_run_returns_200_with_progress(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        active = {
            "run_id": "dream-1",
            "scope": "all",
            "phase": "sweep",
            "checkpoint": {"phase": "sweep", "batch_number": 3, "mutations": 2},
        }
        dream_coordinator.trigger_all_due_projects.return_value = {
            "success": True,
            "run_id": "dream-1",
            "status": "running",
            "coalesced": True,
            "active": active,
        }

        response = dream_client.post("/memory/dream", json={})

        assert response.status_code == 200
        body = response.json()
        assert body["coalesced"] is True
        assert body["run_id"] == "dream-1"
        assert body["active"]["checkpoint"]["batch_number"] == 3

    def test_conflicting_run_returns_409_with_active_details(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        dream_coordinator.trigger.return_value = {
            "success": False,
            "error": "a memory dream run is already active with incompatible options",
            "error_code": "dream_run_conflict",
            "conflict": {"run_id": "other-1", "scope": "project:proj-2", "phase": "sweep"},
        }

        response = dream_client.post("/memory/dream", json={"project_id": "proj-1"})

        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "dream_run_conflict"
        assert body["conflict"]["run_id"] == "other-1"

    def test_launch_failure_returns_500_with_run_id(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        dream_coordinator.trigger.return_value = {
            "success": False,
            "run_id": "dream-1",
            "status": "failed",
            "error": "Failed to launch memory dream run: loop closed",
        }

        response = dream_client.post("/memory/dream", json={"project_id": "proj-1"})

        assert response.status_code == 500
        assert response.json()["run_id"] == "dream-1"

    def test_disabled_dream_returns_400(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        dream_coordinator.trigger_all_due_projects.return_value = {
            "success": False,
            "error": "memory dream is disabled",
        }

        response = dream_client.post("/memory/dream", json={})

        assert response.status_code == 400
        assert response.json()["error"] == "memory dream is disabled"

    def test_status_exposes_durable_checkpoint_fields(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        checkpoint = {
            "phase": "coordinator",
            "scope": "all-due",
            "pass_number": 2,
            "batch_number": 5,
            "completed": 40,
            "remaining": 60,
            "channels": {"keyword": {"attempts": 1, "latency_ms": 42}},
            "mutations": 7,
            "backlog": {"project:proj-1": 60},
            "stop_reason": "window_exhausted",
        }
        dream_coordinator.service.status.return_value = {
            "success": True,
            "run": {"id": "dream-1", "status": "partial", "checkpoint": checkpoint},
        }

        response = dream_client.get("/memory/dream/dream-1")

        assert response.status_code == 200
        assert response.json()["run"]["checkpoint"] == checkpoint
        dream_coordinator.service.status.assert_awaited_once_with("dream-1")

    def test_status_and_revert(
        self, dream_client: TestClient, dream_coordinator: MagicMock
    ) -> None:
        dream_coordinator.service.status.return_value = {
            "success": True,
            "run": {"id": "dream-1"},
        }
        dream_coordinator.service.revert.return_value = {"success": True, "run_id": "dream-1"}

        status = dream_client.get("/memory/dream/dream-1")
        revert = dream_client.post("/memory/dream/dream-1/revert")

        assert status.status_code == 200
        assert revert.status_code == 200
        dream_coordinator.service.status.assert_awaited_once_with("dream-1")
        dream_coordinator.service.revert.assert_awaited_once_with("dream-1")

    def test_missing_coordinator_returns_503(self, mock_server: MagicMock) -> None:
        mock_server.services.memory_dream_coordinator = None
        app = FastAPI()
        app.include_router(create_memory_dream_router(mock_server))

        response = TestClient(app).post("/memory/dream", json={})

        assert response.status_code == 503
        assert response.json()["detail"] == "memory dream coordinator is unavailable"

    def test_route_resolves_coordinator_from_real_service_container(self) -> None:
        """A real ServiceContainer must carry the coordinator field (#19265)."""
        coordinator = MagicMock()
        coordinator.trigger = AsyncMock(
            return_value={
                "success": True,
                "run_id": "dream-1",
                "status": "running",
                "coalesced": False,
            }
        )
        services = ServiceContainer(
            database=MagicMock(),
            session_manager=None,
            task_manager=MagicMock(),
            memory_dream_coordinator=coordinator,
        )
        server = MagicMock()
        server.services = services
        app = FastAPI()
        app.include_router(create_memory_dream_router(server))

        response = TestClient(app).post(
            "/memory/dream", json={"project_id": "proj-1", "dry_run": True}
        )

        assert response.status_code == 202
        assert response.json()["run_id"] == "dream-1"
        coordinator.trigger.assert_awaited_once()


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

    def test_list_rejects_noncanonical_memory_type(self, client, mock_server) -> None:
        response = client.get("/api/memories", params={"memory_type": "debugging_pattern"})

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
                "rationale": _VALID_RATIONALE,
                "memory_type": "preference",
                "project_id": "test-project",
                "tags": ["ui"],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "mm-new-123"
        assert data["content"] == "User prefers dark mode"

    def test_create_memory_supersedes(self, client, mock_server) -> None:
        superseded_id = str(uuid.uuid4())
        mock_server.memory_manager.create_memory = AsyncMock(
            return_value=_make_memory(id="replacement")
        )
        response = client.post(
            "/api/memories",
            json={
                "content": "Replacement",
                "rationale": _VALID_RATIONALE,
                "supersedes": [superseded_id],
            },
        )
        assert response.status_code == 201
        assert mock_server.memory_manager.create_memory.call_args.kwargs["supersedes"] == [
            superseded_id
        ]

    def test_create_memory_supersedes_retry(self, client, mock_server) -> None:
        superseded_id = str(uuid.uuid4())
        mock_server.memory_manager.create_memory = AsyncMock(
            return_value=_make_memory(id="replacement")
        )
        payload = {
            "content": "Replacement",
            "rationale": _VALID_RATIONALE,
            "supersedes": [superseded_id],
        }
        assert client.post("/api/memories", json=payload).status_code == 201
        assert client.post("/api/memories", json=payload).status_code == 201

    def test_create_memory_supersedes_bounds(self, client, mock_server) -> None:
        malformed = client.post(
            "/api/memories",
            json={"content": "Malformed", "supersedes": ["bad-id"]},
        )
        assert malformed.status_code == 422
        over_cap = client.post(
            "/api/memories",
            json={
                "content": "Over cap",
                "supersedes": [str(uuid.uuid4()) for _ in range(21)],
            },
        )
        assert over_cap.status_code == 422
        mock_server.memory_manager.create_memory.assert_not_called()

    def test_create_requires_content(self, client, mock_server) -> None:
        """POST /memories requires content field."""
        response = client.post("/api/memories", json={})
        assert response.status_code == 422

    def test_create_requires_rationale_and_forwards_provenance(self, client, mock_server) -> None:
        missing = client.post("/api/memories", json={"content": "User prefers dark mode"})
        assert missing.status_code == 422
        mock_server.memory_manager.create_memory.assert_not_called()

        mock_server.memory_manager.create_memory = AsyncMock(
            return_value=_make_memory(id="mm-new-123")
        )
        task_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
        response = client.post(
            "/api/memories",
            json={
                "content": "User prefers dark mode",
                "rationale": _VALID_RATIONALE,
                "source_task_id": task_id,
                "created_by_agent": "operator",
            },
        )
        assert response.status_code == 201
        kwargs = mock_server.memory_manager.create_memory.call_args.kwargs
        assert kwargs["rationale"] == _VALID_RATIONALE
        assert kwargs["source_task_id"] == task_id
        assert kwargs["created_by_agent"] == "operator"

    def test_create_rejects_noncanonical_memory_type(self, client, mock_server) -> None:
        response = client.post(
            "/api/memories",
            json={"content": "Bad type", "memory_type": "debugging_pattern"},
        )

        assert response.status_code == 422
        mock_server.memory_manager.create_memory.assert_not_called()

    def test_create_memory_server_error(self, client, mock_server) -> None:
        """POST /memories returns 500 when manager raises error."""
        mock_server.memory_manager.create_memory.side_effect = RuntimeError("Backend failure")
        response = client.post(
            "/api/memories",
            json={"content": "test", "rationale": _VALID_RATIONALE},
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
            json={"content": "Updated content", "memory_type": "preference"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["content"] == "Updated content"
        mock_server.memory_manager.update_memory.assert_awaited_once_with(
            memory_id="mm-abc123",
            content="Updated content",
            tags=None,
            memory_type="preference",
        )

    def test_update_not_found(self, client, mock_server) -> None:
        """PUT /memories/{id} returns 404 when not found."""
        mock_server.memory_manager.update_memory.side_effect = ValueError("Memory not found")
        response = client.put("/api/memories/nonexistent", json={"content": "new content"})
        assert response.status_code == 404

    def test_update_rejects_noncanonical_memory_type(self, client, mock_server) -> None:
        response = client.put(
            "/api/memories/mm-abc123",
            json={"memory_type": "debugging_pattern"},
        )

        assert response.status_code == 422
        mock_server.memory_manager.update_memory.assert_not_called()


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
        promoted = _make_memory(id="mm-promoted", is_global=True)
        mock_server.memory_manager.promote_memory = AsyncMock(return_value=promoted)

        response = client.post("/api/memories/mm-promoted/promote")

        assert response.status_code == 200
        assert response.json()["id"] == "mm-promoted"
        assert response.json()["project_id"] == "test-project"
        assert response.json()["is_global"] is True
        mock_server.memory_manager.promote_memory.assert_awaited_once_with("mm-promoted")

    def test_promote_rejects_extraneous_fields(
        self,
        client: TestClient,
        mock_server: MagicMock,
    ) -> None:
        """Promotion rejects unsupported request fields."""
        promoted = _make_memory(id="mm-promoted", is_global=True)
        mock_server.memory_manager.promote_memory = AsyncMock(return_value=promoted)

        response = client.post(
            "/api/memories/mm-promoted/promote",
            json={"target_project_id": "other-project"},
        )

        assert response.status_code == 422
        mock_server.memory_manager.promote_memory.assert_not_awaited()

    def test_promote_not_found(self, client, mock_server) -> None:
        """A missing memory raises ValueError in storage and surfaces as 404."""
        mock_server.memory_manager.get_memory.return_value = _make_memory(id="nope")
        mock_server.memory_manager.promote_memory = AsyncMock(
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
            params={
                "q": "test",
                "project_id": "proj-1",
                "memory_type": "pattern",
                "limit": 5,
            },
        )
        assert response.status_code == 200
        mock_server.memory_manager.search_memories.assert_called_once_with(
            query="test",
            project_id="proj-1",
            memory_type="pattern",
            limit=5,
            caller="http.memory.search",
        )

    def test_search_rejects_noncanonical_memory_type(self, client, mock_server) -> None:
        response = client.get(
            "/api/memories/search",
            params={"q": "test", "memory_type": "debugging_pattern"},
        )

        assert response.status_code == 422
        mock_server.memory_manager.search_memories.assert_not_called()


# =============================================================================
# GET /memories/stats - statistics
# =============================================================================


class TestMemoryStats:
    """Test GET /memories/stats endpoint."""

    def test_stats_returns_counts(self, client, mock_server) -> None:
        """GET /memories/stats returns memory statistics."""
        mock_server.memory_manager.get_stats = AsyncMock(
            return_value={
                "total_count": 42,
                "by_type": {"fact": 30, "preference": 12},
                "project_id": None,
            }
        )
        response = client.get("/api/memories/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_count"] == 42
        assert data["by_type"]["fact"] == 30

    def test_stats_with_project_filter(self, client, mock_server) -> None:
        """GET /memories/stats supports project_id filter."""
        mock_server.memory_manager.get_stats = AsyncMock(
            return_value={
                "total_count": 10,
                "by_type": {"fact": 10},
                "project_id": "proj-1",
            }
        )
        response = client.get("/api/memories/stats", params={"project_id": "proj-1"})
        assert response.status_code == 200
        mock_server.memory_manager.get_stats.assert_awaited_once_with(project_id="proj-1")

    def test_stats_server_error(self, client, mock_server) -> None:
        """GET /memories/stats returns 500 on error."""
        mock_server.memory_manager.get_stats = AsyncMock(side_effect=RuntimeError("DB error"))
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
        mock_server.memory_manager.storage.get_all_crossrefs.assert_called_once_with(
            scope=MemoryScope.project_visible("proj-1"),
            limit=2000,
        )

    @pytest.mark.parametrize("memory_limit", [-1, 1001])
    def test_graph_rejects_out_of_range_limit(self, client, memory_limit: int) -> None:
        response = client.get("/api/memories/graph", params={"memory_limit": memory_limit})

        assert response.status_code == 422

    @pytest.mark.parametrize("memory_limit", [1, 1000])
    def test_graph_accepts_boundary_limit(self, client, mock_server, memory_limit: int) -> None:
        mock_server.memory_manager.list_memories.return_value = []
        mock_server.memory_manager.storage.get_all_crossrefs.return_value = []

        response = client.get("/api/memories/graph", params={"memory_limit": memory_limit})

        assert response.status_code == 200
        mock_server.memory_manager.list_memories.assert_called_once_with(
            project_id=None, limit=memory_limit
        )
        mock_server.memory_manager.storage.get_all_crossrefs.assert_called_once_with(
            scope=ALL_MEMORIES,
            limit=memory_limit * 10,
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
        mock_server.memory_manager.get_entity_graph.assert_awaited_once_with(
            limit=500, relationship_limit=2000, project_id=None
        )

    def test_entity_graph_passes_limits_through(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Zero uses the configured/default ceiling while bounded values pass through."""
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_graph = AsyncMock(
            return_value={"entities": [], "relationships": []}
        )
        response = client.get("/api/memories/graph/entities?limit=0&relationship_limit=12")
        assert response.status_code == 200
        mock_server.memory_manager.get_entity_graph.assert_awaited_once_with(
            limit=500, relationship_limit=12, project_id=None
        )

    def test_entity_graph_caps_requests_at_operator_configured_limits(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        mock_server.config.ui = SimpleNamespace(
            knowledge_graph_limit=7,
            knowledge_graph_relationship_limit=13,
        )
        mock_server.memory_manager._falkor_client = MagicMock()
        mock_server.memory_manager.get_entity_graph = AsyncMock(
            return_value={"entities": [], "relationships": []}
        )

        response = client.get("/api/memories/graph/entities?limit=99&relationship_limit=0")

        assert response.status_code == 200
        mock_server.memory_manager.get_entity_graph.assert_awaited_once_with(
            limit=7,
            relationship_limit=13,
            project_id=None,
        )

    def test_entity_graph_rejects_negative_limits(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        """Negative limits fail validation (0 is the unlimited sentinel)."""
        mock_server.memory_manager._falkor_client = MagicMock()
        response = client.get("/api/memories/graph/entities?limit=-1")
        assert response.status_code == 422

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

    def test_delete_server_error(self, client: TestClient, mock_server: MagicMock) -> None:
        """DELETE /memories/{id} returns 500 on error."""
        mock_server.memory_manager.delete_memory = AsyncMock(side_effect=RuntimeError("DB error"))
        response = client.delete("/api/memories/mm-abc123")
        assert response.status_code == 500

    def test_search_server_error(self, client: TestClient, mock_server: MagicMock) -> None:
        """GET /memories/search returns 500 on error."""
        mock_server.memory_manager.search_memories = AsyncMock(
            side_effect=RuntimeError("Search error")
        )
        response = client.get("/api/memories/search", params={"q": "test"})
        assert response.status_code == 500
