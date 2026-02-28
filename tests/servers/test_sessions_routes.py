"""
Comprehensive tests for session routes HTTP handlers.

This module tests edge cases, error paths, and validation that are not
covered by the existing test_http_server.py tests.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def session_storage(temp_db: LocalDatabase) -> LocalSessionManager:
    """Create session storage."""
    return LocalSessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: LocalDatabase) -> LocalProjectManager:
    """Create project storage."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def test_project(project_storage: LocalProjectManager, temp_dir: Path) -> dict[str, Any]:
    """Create a test project with project.json file."""
    project = project_storage.create(name="test-project", repo_path=str(temp_dir))

    # Create .gobby/project.json for project resolution
    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir()
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    return project.to_dict()


@pytest.fixture
def http_server(
    session_storage: LocalSessionManager,
    temp_dir: Path,
) -> HTTPServer:
    """Create an HTTP server instance for testing."""
    mock_config = MagicMock()
    mock_config.logging.max_size_mb = 10
    mock_config.logging.backup_count = 3
    mock_config.memory.backend = "null"
    mock_config.workflow.timeout = 30
    mock_config.workflow.enabled = True
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    services = ServiceContainer(
        config=mock_config,
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
    )
    return HTTPServer(
        services=services,
        port=60887,
        test_mode=True,
    )


@pytest.fixture
def client(http_server: HTTPServer) -> TestClient:
    """Create a test client for the HTTP server."""
    return TestClient(http_server.app)


# ============================================================================
# Session Registration Tests - Error Paths
# ============================================================================


class TestRegisterSessionEdgeCases:
    """Tests for register_session edge cases and error paths."""

    def test_register_with_project_path_extracts_git_branch(
        self,
        client: TestClient,
        test_project: dict[str, Any],
        temp_dir: Path,
    ) -> None:
        """Test that git_branch is extracted from project_path when not provided."""
        with (
            patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine"),
            patch("gobby.utils.git.get_git_metadata") as mock_git,
        ):
            mock_git.return_value = {"git_branch": "feature/extracted-branch"}

            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "git-branch-test",
                    "source": "claude",
                    "project_path": str(temp_dir),
                    "cwd": str(temp_dir),
                },
            )

        assert response.status_code == 200
        mock_git.assert_called_once_with(str(temp_dir))

    def test_register_with_project_path_no_git_branch_in_metadata(
        self,
        client: TestClient,
        test_project: dict[str, Any],
        temp_dir: Path,
    ) -> None:
        """Test registration when git metadata has no branch."""
        with (
            patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine"),
            patch("gobby.utils.git.get_git_metadata") as mock_git,
        ):
            # Return empty dict - no git_branch key
            mock_git.return_value = {}

            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "no-git-branch-test",
                    "source": "claude",
                    "project_path": str(temp_dir),
                    "cwd": str(temp_dir),
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "registered"

    def test_register_with_explicit_git_branch_skips_extraction(
        self,
        client: TestClient,
        test_project: dict[str, Any],
        temp_dir: Path,
    ) -> None:
        """Test that explicit git_branch skips metadata extraction."""
        with (
            patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine"),
            patch("gobby.utils.git.get_git_metadata") as mock_git,
        ):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "explicit-branch-test",
                    "source": "claude",
                    "project_path": str(temp_dir),
                    "git_branch": "explicit/branch",
                    "cwd": str(temp_dir),
                },
            )

        assert response.status_code == 200
        # git extraction should not be called when git_branch is provided
        mock_git.assert_not_called()

    def test_register_session_internal_error(
        self,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
        temp_dir: Path,
    ) -> None:
        """Test that internal errors during registration return 500."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        with (
            patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine"),
            patch.object(session_storage, "register", side_effect=RuntimeError("Database error")),
        ):
            response = test_client.post(
                "/api/sessions/register",
                json={
                    "external_id": "error-test",
                    "source": "claude",
                    "project_id": test_project["id"],
                },
            )

        assert response.status_code == 500
        data = response.json()
        assert "Internal server error" in data["detail"]

    def test_register_machine_id_fallback_to_unknown(
        self,
        client: TestClient,
        test_project: dict[str, Any],
    ) -> None:
        """Test that machine_id falls back to 'unknown-machine' when get_machine_id returns None."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value=None):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "unknown-machine-test",
                    "source": "claude",
                    "project_id": test_project["id"],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["machine_id"] == "unknown-machine"


# ============================================================================
# List Sessions Tests - Error Paths
# ============================================================================


class TestListSessionsEdgeCases:
    """Tests for list_sessions edge cases and error paths."""

    def test_list_sessions_message_count_failure(
        self,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test that message count failure is handled gracefully."""
        # Create a session first
        session_storage.register(
            external_id="list-msg-fail",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        # Create server with mock message manager that fails
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        # Add a failing message_manager
        mock_message_manager = AsyncMock()
        mock_message_manager.get_all_counts = AsyncMock(
            side_effect=RuntimeError("Message store unavailable")
        )
        server.message_manager = mock_message_manager

        test_client = TestClient(server.app)
        response = test_client.get("/api/sessions")

        # Should still succeed, just without message counts
        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data
        # Message count should default to 0 when fetch fails
        for session in data["sessions"]:
            assert session["message_count"] == 0

    def test_list_sessions_internal_error(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test that internal errors during list return 500."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        with patch.object(session_storage, "list", side_effect=RuntimeError("Database error")):
            response = test_client.get("/api/sessions")

        assert response.status_code == 500


# ============================================================================
# Get Session Tests - Error Paths
# ============================================================================


class TestGetSessionEdgeCases:
    """Tests for sessions_get edge cases and error paths."""

    def test_get_session_internal_error(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test that internal errors during get return 500."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        with patch.object(session_storage, "get", side_effect=RuntimeError("Database error")):
            response = test_client.get("/api/sessions/some-session-id")

        assert response.status_code == 500

    def test_get_session_without_session_manager(self) -> None:
        """Test getting session when session manager is None returns 503."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=None,
        )
        test_client = TestClient(server.app)

        response = test_client.get("/api/sessions/any-session-id")
        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]


# ============================================================================
# Get Messages Tests - Error Paths
# ============================================================================


class TestGetMessagesEdgeCases:
    """Tests for sessions_get_messages edge cases and error paths."""

    def test_get_messages_with_all_parameters(
        self,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test get_messages with all optional parameters."""
        # Create a session
        session = session_storage.register(
            external_id="messages-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        # Add a mock message_manager
        mock_message_manager = AsyncMock()
        mock_message_manager.get_messages = AsyncMock(return_value=[])
        mock_message_manager.count_messages = AsyncMock(return_value=0)
        server.message_manager = mock_message_manager

        test_client = TestClient(server.app)

        response = test_client.get(f"/api/sessions/{session.id}/messages?limit=50&offset=10&role=user")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "messages" in data
        assert "total_count" in data
        assert "response_time_ms" in data

        # Verify the parameters were passed correctly
        mock_message_manager.get_messages.assert_called_once_with(
            session_id=session.id, limit=50, offset=10, role="user"
        )

    def test_get_messages_internal_error(
        self,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test that internal errors during get_messages return 500."""
        session = session_storage.register(
            external_id="messages-error-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )

        # Add a failing message_manager
        mock_message_manager = AsyncMock()
        mock_message_manager.get_messages = AsyncMock(side_effect=RuntimeError("Database error"))
        server.message_manager = mock_message_manager

        test_client = TestClient(server.app)
        response = test_client.get(f"/api/sessions/{session.id}/messages")

        assert response.status_code == 500


# ============================================================================
# Find Current Session Tests - Error Paths
# ============================================================================


class TestFindCurrentSessionEdgeCases:
    """Tests for find_current_session edge cases and error paths."""

    def test_find_current_session_without_session_manager(self) -> None:
        """Test find_current when session manager is None returns 503."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=None,
        )
        test_client = TestClient(server.app)

        response = test_client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "test",
                "machine_id": "machine",
                "source": "claude",
                "project_id": "test-project",
            },
        )
        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]

    def test_find_current_session_missing_project_id(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test find_current without project_id or cwd returns 400."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        response = test_client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "test",
                "machine_id": "machine",
                "source": "claude",
            },
        )
        assert response.status_code == 400
        assert "project_id or cwd" in response.json()["detail"]

    def test_find_current_session_internal_error(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test that internal errors during find_by_external_id return 500."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        with patch.object(
            session_storage, "find_by_external_id", side_effect=RuntimeError("Database error")
        ):
            response = test_client.post(
                "/api/sessions/find_current",
                json={
                    "external_id": "test",
                    "machine_id": "machine",
                    "source": "claude",
                    "project_id": "test-project",
                },
            )

        assert response.status_code == 500


# ============================================================================
# Find Parent Session Tests - Error Paths
# ============================================================================


class TestFindParentSessionEdgeCases:
    """Tests for find_parent_session edge cases and error paths."""

    def test_find_parent_without_session_manager(self) -> None:
        """Test find_parent when session manager is None returns 503."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=None,
        )
        test_client = TestClient(server.app)

        response = test_client.post(
            "/api/sessions/find_parent",
            json={
                "source": "claude",
                "machine_id": "test-machine",
                "project_id": "proj-123",
            },
        )
        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]

    def test_find_parent_machine_id_fallback(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test find_parent falls back to machine_id when not provided."""
        # Create a session with handoff_ready status
        session = session_storage.register(
            external_id="parent-fallback-test",
            machine_id="test-machine-fallback",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.update_status(session.id, "handoff_ready")

        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine-fallback"):
            response = client.post(
                "/api/sessions/find_parent",
                json={
                    "source": "claude",
                    # No machine_id - should be resolved via get_machine_id
                    "project_id": test_project["id"],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == session.id

    def test_find_parent_machine_id_unknown_fallback(
        self,
        client: TestClient,
        test_project: dict[str, Any],
    ) -> None:
        """Test find_parent uses 'unknown-machine' when get_machine_id returns None."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value=None):
            response = client.post(
                "/api/sessions/find_parent",
                json={
                    "source": "claude",
                    "project_id": test_project["id"],
                },
            )

        # Should succeed (returning no session) but not error
        assert response.status_code == 200
        data = response.json()
        assert data["session"] is None

    def test_find_parent_internal_error(
        self,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test that internal errors during find_parent return 500."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        with patch.object(
            session_storage, "find_parent", side_effect=RuntimeError("Database error")
        ):
            response = test_client.post(
                "/api/sessions/find_parent",
                json={
                    "source": "claude",
                    "machine_id": "machine",
                    "project_id": test_project["id"],
                },
            )

        assert response.status_code == 500


# ============================================================================
# Update Status Tests - Error Paths
# ============================================================================


class TestUpdateStatusEdgeCases:
    """Tests for update_session_status edge cases and error paths."""

    def test_update_status_without_session_manager(self) -> None:
        """Test update_status when session manager is None returns 503."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=None,
        )
        test_client = TestClient(server.app)

        response = test_client.post(
            "/api/sessions/update_status",
            json={
                "session_id": "test-id",
                "status": "paused",
            },
        )
        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]

    def test_update_status_internal_error(
        self,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test that internal errors during update_status return 500."""
        session = session_storage.register(
            external_id="status-error-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        with patch.object(
            session_storage, "update_status", side_effect=RuntimeError("Database error")
        ):
            response = test_client.post(
                "/api/sessions/update_status",
                json={
                    "session_id": session.id,
                    "status": "paused",
                },
            )

        assert response.status_code == 500


# ============================================================================
# Update Summary Tests - Error Paths
# ============================================================================


class TestUpdateSummaryEdgeCases:
    """Tests for update_session_summary edge cases and error paths."""

    def test_update_summary_without_session_manager(self) -> None:
        """Test update_summary when session manager is None returns 503."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=None,
        )
        test_client = TestClient(server.app)

        response = test_client.post(
            "/api/sessions/update_summary",
            json={
                "session_id": "test-id",
                "summary_path": "/path/to/summary.md",
            },
        )
        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]

    def test_update_summary_internal_error(
        self,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test that internal errors during update_summary return 500."""
        session = session_storage.register(
            external_id="summary-error-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        test_client = TestClient(server.app)

        with patch.object(
            session_storage, "update_summary", side_effect=RuntimeError("Database error")
        ):
            response = test_client.post(
                "/api/sessions/update_summary",
                json={
                    "session_id": session.id,
                    "summary_path": "/path/to/summary.md",
                },
            )

        assert response.status_code == 500


# ============================================================================
# Stop Signal Tests - Additional Error Paths
# ============================================================================


class FakeStopSignal:
    """Fake stop signal for testing."""

    def __init__(
        self,
        signal_id: str = "sig-123",
        reason: str = "Test stop",
        source: str = "http_api",
    ) -> None:
        self.signal_id = signal_id
        self.reason = reason
        self.source = source
        self.signaled_at = datetime.now(UTC)
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


class TestStopSignalEdgeCases:
    """Additional tests for stop signal error paths."""

    @pytest.fixture
    def server_with_stop_registry(
        self,
        session_storage: LocalSessionManager,
    ) -> HTTPServer:
        """Create HTTP server with mock stop registry."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        server.app.state.hook_manager = FakeHookManager()
        return server

    @pytest.fixture
    def stop_client(self, server_with_stop_registry: HTTPServer) -> TestClient:
        """Create test client with stop registry."""
        return TestClient(server_with_stop_registry.app)

    def test_post_stop_signal_internal_error(
        self,
        server_with_stop_registry: HTTPServer,
    ) -> None:
        """Test that internal errors during stop signal return 500."""
        # Make the stop registry raise an error
        server_with_stop_registry.app.state.hook_manager._stop_registry.signal_stop = MagicMock(
            side_effect=RuntimeError("Signal error")
        )

        test_client = TestClient(server_with_stop_registry.app)
        response = test_client.post(
            "/api/sessions/test-session/stop",
            json={"reason": "Test stop"},
        )

        assert response.status_code == 500

    def test_get_stop_signal_internal_error(
        self,
        server_with_stop_registry: HTTPServer,
    ) -> None:
        """Test that internal errors during get stop signal return 500."""
        # Make the stop registry raise an error
        server_with_stop_registry.app.state.hook_manager._stop_registry.get_signal = MagicMock(
            side_effect=RuntimeError("Signal lookup error")
        )

        test_client = TestClient(server_with_stop_registry.app)
        response = test_client.get("/api/sessions/test-session/stop")

        assert response.status_code == 500

    def test_delete_stop_signal_internal_error(
        self,
        server_with_stop_registry: HTTPServer,
    ) -> None:
        """Test that internal errors during delete stop signal return 500."""
        # Make the stop registry raise an error
        server_with_stop_registry.app.state.hook_manager._stop_registry.clear = MagicMock(
            side_effect=RuntimeError("Clear error")
        )

        test_client = TestClient(server_with_stop_registry.app)
        response = test_client.delete("/api/sessions/test-session/stop")

        assert response.status_code == 500

    def test_get_stop_signal_without_hook_manager(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test GET stop signal when hook manager not available."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        # No hook_manager on app.state

        test_client = TestClient(server.app)
        response = test_client.get("/api/sessions/test-session/stop")

        assert response.status_code == 503
        assert "Hook manager not available" in response.json()["detail"]

    def test_delete_stop_signal_without_hook_manager(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test DELETE stop signal when hook manager not available."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        # No hook_manager on app.state

        test_client = TestClient(server.app)
        response = test_client.delete("/api/sessions/test-session/stop")

        assert response.status_code == 503
        assert "Hook manager not available" in response.json()["detail"]

    def test_get_stop_signal_without_stop_registry(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test GET stop signal when stop registry not available."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        # Set hook_manager without stop_registry
        server.app.state.hook_manager = MagicMock()
        server.app.state.hook_manager._stop_registry = None

        test_client = TestClient(server.app)
        response = test_client.get("/api/sessions/test-session/stop")

        assert response.status_code == 503
        assert "Stop registry not available" in response.json()["detail"]

    def test_delete_stop_signal_without_stop_registry(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test DELETE stop signal when stop registry not available."""
        server = create_http_server(
            port=60887,
            test_mode=True,
            session_manager=session_storage,
        )
        # Set hook_manager without stop_registry
        server.app.state.hook_manager = MagicMock()
        server.app.state.hook_manager._stop_registry = None

        test_client = TestClient(server.app)
        response = test_client.delete("/api/sessions/test-session/stop")

        assert response.status_code == 503
        assert "Stop registry not available" in response.json()["detail"]

    def test_get_stop_signal_with_acknowledged(
        self,
        server_with_stop_registry: HTTPServer,
    ) -> None:
        """Test GET stop signal includes acknowledgement details."""
        # Create a signal and acknowledge it
        registry = server_with_stop_registry.app.state.hook_manager._stop_registry
        signal = registry.signal_stop("ack-session", reason="Test", source="test")
        signal.acknowledged = True
        signal.acknowledged_at = datetime.now(UTC)

        test_client = TestClient(server_with_stop_registry.app)
        response = test_client.get("/api/sessions/ack-session/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["has_signal"] is True
        assert data["acknowledged"] is True
        assert data["acknowledged_at"] is not None


# ============================================================================
# Request Validation Tests
# ============================================================================


class TestRequestValidation:
    """Tests for request validation."""

    def test_register_missing_external_id(self, client: TestClient) -> None:
        """Test that registration fails without external_id."""
        response = client.post(
            "/api/sessions/register",
            json={
                "source": "claude",
            },
        )
        # Pydantic validation should fail
        assert response.status_code == 422

    def test_list_sessions_invalid_limit(self, client: TestClient) -> None:
        """Test that list_sessions validates limit parameter."""
        # Limit too low
        response = client.get("/api/sessions?limit=0")
        assert response.status_code == 422

        # Limit too high
        response = client.get("/api/sessions?limit=10000")
        assert response.status_code == 422

    def test_list_sessions_valid_limit_bounds(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test list_sessions with valid limit bounds."""
        # Create a session for listing
        session_storage.register(
            external_id="limit-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        # Minimum valid limit
        response = client.get("/api/sessions?limit=1")
        assert response.status_code == 200

        # Maximum valid limit
        response = client.get("/api/sessions?limit=1000")
        assert response.status_code == 200


# ============================================================================
# _sanitize_title unit tests (#9217)
# ============================================================================


class TestSanitizeTitle:
    """Tests for the _sanitize_title helper."""

    def setup_method(self) -> None:
        from gobby.servers.routes.sessions import _sanitize_title

        self.sanitize = _sanitize_title

    def test_strips_markdown(self) -> None:
        assert self.sanitize("**Bold** and _italic_") == "Bold and italic"
        assert self.sanitize("# Heading") == "Heading"
        assert self.sanitize("[link](url)") == "linkurl"

    def test_strips_emoji(self) -> None:
        assert self.sanitize("\U0001f680 Rocket Launch") == "Rocket Launch"
        assert self.sanitize("Hello \U0001f600\U0001f600\U0001f600") == "Hello"
        assert self.sanitize("\U0001f525\U0001f4a5") == "Untitled Session"

    def test_strips_quotes(self) -> None:
        assert self.sanitize('"Quoted Title"') == "Quoted Title"
        assert self.sanitize("'Single Quoted'") == "Single Quoted"

    def test_first_line_only(self) -> None:
        assert self.sanitize("First\nSecond\nThird") == "First"

    def test_caps_at_100(self) -> None:
        long = "A" * 150
        result = self.sanitize(long)
        assert len(result) == 100
        assert result.endswith("...")

    def test_normalizes_whitespace(self) -> None:
        assert self.sanitize("  too   many   spaces  ") == "too many spaces"

    def test_empty_returns_untitled(self) -> None:
        assert self.sanitize("") == "Untitled Session"
        assert self.sanitize("   ") == "Untitled Session"

    def test_plain_text_unchanged(self) -> None:
        assert self.sanitize("Fix the login bug") == "Fix the login bug"

    def test_combined_junk(self) -> None:
        result = self.sanitize('  "## \U0001f680 **My Title**"  ')
        assert result == "My Title"
