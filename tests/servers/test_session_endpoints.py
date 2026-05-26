"""Tests for HTTP session endpoints."""

from collections.abc import Iterator
from datetime import UTC
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestSessionEndpoints:
    """Tests for session endpoints."""

    def test_get_session(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Test getting a session by ID."""
        # Register a session first
        session = session_storage.register(
            external_id="get-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        response = client.get(f"/api/sessions/{session.id}")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "success"
        assert data["session"]["external_id"] == "get-test"

    def test_get_session_not_found(self, client: TestClient) -> None:
        """Test getting nonexistent session returns 404."""
        response = client.get("/api/sessions/nonexistent-uuid")
        assert response.status_code == 404

    def test_find_current_session_missing_fields(self, client: TestClient) -> None:
        """Test find_current with missing required fields."""
        response = client.post(
            "/api/sessions/find_current",
            json={"external_id": "test"},
        )

        assert response.status_code == 400

    def test_update_session_status(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Test updating session status."""
        session = session_storage.register(
            external_id="status-update",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        response = client.post(
            "/api/sessions/update_status",
            json={
                "session_id": session.id,
                "status": "paused",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["status"] == "paused"

    def test_update_session_status_not_found(self, client: TestClient) -> None:
        """Test updating status of nonexistent session."""
        response = client.post(
            "/api/sessions/update_status",
            json={
                "session_id": "nonexistent-uuid",
                "status": "paused",
            },
        )

        assert response.status_code == 404

    def test_update_session_summary(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Test updating session summary."""
        session = session_storage.register(
            external_id="summary-update",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        response = client.post(
            "/api/sessions/update_summary",
            json={
                "session_id": session.id,
                "summary_path": "/path/to/summary.md",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["summary_path"] == "/path/to/summary.md"

    def test_update_session_summary_not_found(self, client: TestClient) -> None:
        """Test updating summary of nonexistent session."""
        response = client.post(
            "/api/sessions/update_summary",
            json={
                "session_id": "nonexistent-uuid",
                "summary_path": "/path/to/summary.md",
            },
        )

        assert response.status_code == 404

    def test_list_sessions(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Test listing sessions."""
        # Create a few sessions
        session_storage.register(
            external_id="list-test-1",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.register(
            external_id="list-test-2",
            machine_id="machine",
            source="gemini",
            project_id=test_project["id"],
        )

        response = client.get("/api/sessions")
        assert response.status_code == 200

        data = response.json()
        assert "sessions" in data
        assert "count" in data
        assert data["count"] >= 2
        assert "response_time_ms" in data

    def test_list_sessions_with_filters(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict,
    ) -> None:
        """Test listing sessions with query filters."""
        session_storage.register(
            external_id="filter-test-1",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.register(
            external_id="filter-test-2",
            machine_id="machine",
            source="gemini",
            project_id=test_project["id"],
        )

        # Filter by source
        response = client.get(f"/api/sessions?source=claude&project_id={test_project['id']}")
        assert response.status_code == 200

        data = response.json()
        assert "sessions" in data
        # All returned sessions should be claude source
        for session in data["sessions"]:
            assert session["source"] == "claude"

    def test_get_messages_without_manager(self, client: TestClient) -> None:
        """Test getting messages when transcript reader not available."""
        response = client.get("/api/sessions/test-session/messages")
        assert response.status_code == 503
        assert "Transcript reader not available" in response.json()["detail"]

    def test_list_sessions_without_manager(
        self,
        session_storage: SessionManager,
    ) -> None:
        """Test listing sessions when session manager is None returns 503."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=None,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )
        client = TestClient(server.app)
        response = client.get("/api/sessions")
        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]

    def test_register_without_manager(self, session_storage: SessionManager) -> None:
        """Test registering when session manager is None returns 503."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=None,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )
        client = TestClient(server.app)
        response = client.post(
            "/api/sessions/register",
            json={"external_id": "test", "source": "claude"},
        )
        assert response.status_code == 503

    def test_find_parent_missing_source(self, client: TestClient) -> None:
        """Test find_parent with missing source field."""
        response = client.post(
            "/api/sessions/find_parent",
            json={"machine_id": "test-machine"},
        )

        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_find_current_malformed_json(self, client: TestClient) -> None:
        """Test find_current with malformed JSON returns 500 error.

        The route's exception handler catches JSONDecodeError and raises
        HTTPException with status 500 before the global handler runs.
        """
        response = client.post(
            "/api/sessions/find_current",
            content="{ invalid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 500
        data = response.json()
        assert "detail" in data

    def test_find_parent_malformed_json(self, client: TestClient) -> None:
        """Test find_parent with malformed JSON returns 500 error.

        The route's exception handler catches JSONDecodeError and raises
        HTTPException with status 500 before the global handler runs.
        """
        response = client.post(
            "/api/sessions/find_parent",
            content="not valid json {",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 500
        data = response.json()
        assert "detail" in data

    def test_update_status_malformed_json(self, client: TestClient) -> None:
        """Test update_status with malformed JSON returns 500 error.

        The route's exception handler catches JSONDecodeError and raises
        HTTPException with status 500 before the global handler runs.
        """
        response = client.post(
            "/api/sessions/update_status",
            content="[broken",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 500
        data = response.json()
        assert "detail" in data

    def test_update_summary_malformed_json(self, client: TestClient) -> None:
        """Test update_summary with malformed JSON returns 500 error.

        The route's exception handler catches JSONDecodeError and raises
        HTTPException with status 500 before the global handler runs.
        """
        response = client.post(
            "/api/sessions/update_summary",
            content="{incomplete",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 500
        data = response.json()
        assert "detail" in data

    def test_update_status_missing_fields(self, client: TestClient) -> None:
        """Test update_status with missing required fields."""
        response = client.post(
            "/api/sessions/update_status",
            json={"session_id": "test-id"},  # missing status
        )
        assert response.status_code == 400

    def test_update_summary_missing_fields(self, client: TestClient) -> None:
        """Test update_summary with missing required fields."""
        response = client.post(
            "/api/sessions/update_summary",
            json={"session_id": "test-id"},  # missing summary_path
        )
        assert response.status_code == 400


class FakeStopSignal:
    """Fake stop signal for testing."""

    def __init__(
        self,
        signal_id: str = "sig-123",
        reason: str = "Test stop",
        source: str = "http_api",
    ) -> None:
        from datetime import datetime

        self.session_id = signal_id
        self.reason = reason
        self.source = source
        self.requested_at = datetime.now(UTC)
        self.acknowledged = False
        self.acknowledged_at = None


class FakeStopRegistry:
    """Fake stop registry for testing."""

    def __init__(self) -> None:
        self._signals: dict[str, FakeStopSignal] = {}

    def signal_stop(
        self, session_id: str, reason: str = "Test", source: str = "test"
    ) -> FakeStopSignal:
        signal = FakeStopSignal(reason=reason, source=source)
        self._signals[session_id] = signal
        return signal

    def get_signal(self, session_id: str) -> FakeStopSignal | None:
        return self._signals.get(session_id)

    def clear(self, session_id: str) -> bool:
        if session_id in self._signals:
            del self._signals[session_id]
            return True
        return False


class FakeHookManager:
    """Fake hook manager for testing stop signal endpoints."""

    def __init__(self) -> None:
        self._stop_registry = FakeStopRegistry()

    def shutdown(self) -> None:
        pass

    async def shutdown_async(self) -> None:
        pass


class TestStopSignalEndpoints:
    """Tests for stop signal HTTP endpoints."""

    @pytest.fixture
    def server_with_stop_registry(
        self,
        session_storage: SessionManager,
    ) -> HTTPServer:
        """Create HTTP server with mock stop registry."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            mcp_manager=None,
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )
        # Mock the hook_manager in app state
        server.app.state.hook_manager = FakeHookManager()
        return server

    @pytest.fixture
    def stop_client(self, server_with_stop_registry: HTTPServer) -> Iterator[TestClient]:
        """Create test client with stop registry."""
        with TestClient(server_with_stop_registry.app) as client:
            # Re-apply FakeHookManager after lifespan initialization
            client.app.state.hook_manager = FakeHookManager()
            yield client

    def test_post_stop_signal(self, stop_client: TestClient) -> None:
        """Test sending a stop signal to a session."""
        response = stop_client.post(
            "/api/sessions/test-session-123/stop",
            json={"reason": "User requested stop", "source": "dashboard"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "stop_signaled"
        assert data["session_id"] == "test-session-123"
        assert data["reason"] == "User requested stop"
        assert data["source"] == "dashboard"
        assert "signal_id" not in data
        assert "signaled_at" in data

    def test_post_stop_signal_default_values(self, stop_client: TestClient) -> None:
        """Test stop signal with default reason and source."""
        response = stop_client.post("/api/sessions/test-session-456/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "stop_signaled"
        assert data["reason"] == "External stop request"
        assert data["source"] == "http_api"

    def test_get_stop_signal_present(
        self, stop_client: TestClient, server_with_stop_registry: HTTPServer
    ) -> None:
        """Test checking for existing stop signal."""
        # First send a signal
        stop_client.post("/api/sessions/check-session/stop", json={"reason": "Test"})

        # Then check for it
        response = stop_client.get("/api/sessions/check-session/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["has_signal"] is True
        assert data["session_id"] == "check-session"
        assert "signal_id" not in data
        assert "reason" in data

    def test_get_stop_signal_absent(self, stop_client: TestClient) -> None:
        """Test checking for non-existent stop signal."""
        response = stop_client.get("/api/sessions/no-signal-session/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["has_signal"] is False
        assert data["session_id"] == "no-signal-session"

    def test_delete_stop_signal(self, stop_client: TestClient) -> None:
        """Test clearing a stop signal."""
        # First send a signal
        stop_client.post("/api/sessions/clear-session/stop")

        # Then clear it
        response = stop_client.delete("/api/sessions/clear-session/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "cleared"
        assert data["was_present"] is True

        # Verify it's gone
        check_response = stop_client.get("/api/sessions/clear-session/stop")
        assert check_response.json()["has_signal"] is False

    def test_delete_stop_signal_not_present(self, stop_client: TestClient) -> None:
        """Test clearing non-existent stop signal."""
        response = stop_client.delete("/api/sessions/no-signal/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "no_signal"
        assert data["was_present"] is False

    def test_stop_signal_without_hook_manager(self, client: TestClient) -> None:
        """Test stop signal endpoints when hook manager not available."""
        response = client.post("/api/sessions/test-session/stop")
        assert response.status_code == 503
        assert "Hook manager not available" in response.json()["detail"]

    def test_stop_signal_without_stop_registry(self, session_storage: SessionManager) -> None:
        """Test stop signal endpoints when stop registry not available."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )
        # Set hook_manager without stop_registry
        with TestClient(server.app) as client:
            # Overwrite after lifespan
            mock_hm = FakeHookManager()
            mock_hm._stop_registry = None
            client.app.state.hook_manager = mock_hm

            response = client.post("/api/sessions/test-session/stop")

        assert response.status_code == 503
        assert "Stop registry not available" in response.json()["detail"]
