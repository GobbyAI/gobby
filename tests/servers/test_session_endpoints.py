"""Tests for HTTP session endpoints."""

from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.http import HTTPServer
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("authenticated_http_requests", "isolated_http_runtime"),
]

# Valid-format UUID that doesn't exist in the database.
UNKNOWN_SESSION_ID = "99999999-9999-4999-8999-999999999999"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


class TestSessionEndpoints:
    """Tests for session endpoints."""

    def test_get_session(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test getting a session by ID."""
        # Register a session first
        session = session_storage.register(
            external_id="get-test",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )

        response = client.get(f"/api/sessions/{session.id}")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "success"
        assert data["session"]["external_id"] == "get-test"

    def test_bulk_move_renumbers_and_broadcasts_only_committed_sessions(
        self,
        client: TestClient,
        http_server: HTTPServer,
        session_storage: SessionManager,
        test_project: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        destination = LocalProjectManager(session_storage.db).create(
            name="bulk-move-destination",
            repo_path=str(tmp_path / "bulk-move-destination"),
        )
        for index in range(2):
            session_storage.register(
                external_id=f"destination-{index}",
                machine_id="21000000-0000-4000-8000-000000000003",
                source="codex",
                project_id=destination.id,
            )
        moved_sessions = [
            session_storage.register(
                external_id=f"source-{index}",
                machine_id="21000000-0000-4000-8000-000000000003",
                source="codex",
                project_id=test_project["id"],
            )
            for index in range(2)
        ]
        websocket = AsyncMock()
        http_server.services.websocket_server = websocket
        session_storage.db.execute(
            """
            CREATE OR REPLACE FUNCTION fail_first_bulk_move_fn()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF OLD.external_id = 'source-0' THEN
                    RAISE EXCEPTION 'bulk move boom';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        session_storage.db.execute(
            """
            CREATE TRIGGER fail_first_bulk_move
            BEFORE UPDATE OF project_id ON sessions
            FOR EACH ROW
            EXECUTE FUNCTION fail_first_bulk_move_fn()
            """
        )
        try:
            response = client.post(
                "/api/sessions/bulk-move",
                json={
                    "session_ids": [
                        moved_sessions[0].id,
                        UNKNOWN_SESSION_ID,
                        moved_sessions[1].id,
                    ],
                    "target_project_id": destination.id,
                },
            )
        finally:
            session_storage.db.execute("DROP TRIGGER IF EXISTS fail_first_bulk_move ON sessions")
            session_storage.db.execute("DROP FUNCTION IF EXISTS fail_first_bulk_move_fn()")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["moved"] == 1
        assert data["total"] == 3
        assert len(data["errors"]) == 2
        assert data["errors"][0].startswith(
            f"Failed to move {moved_sessions[0].id}: bulk move boom"
        )
        assert data["errors"][1] == f"Session {UNKNOWN_SESSION_ID} not found"
        reloaded = [session_storage.get(session.id) for session in moved_sessions]
        assert [(session.project_id, session.seq_num) for session in reloaded if session] == [
            (test_project["id"], 1),
            (destination.id, 3),
        ]
        assert websocket.broadcast_session_event.await_args_list == [
            call("session_updated", moved_sessions[1].id),
        ]

    def test_get_session_not_found(self, client: TestClient) -> None:
        """Test getting nonexistent session returns 404."""
        response = client.get(f"/api/sessions/{UNKNOWN_SESSION_ID}")
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
        test_project: dict[str, Any],
    ) -> None:
        """Test updating session status."""
        session = session_storage.register(
            external_id="status-update",
            machine_id="21000000-0000-4000-8000-000000000003",
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
                "session_id": UNKNOWN_SESSION_ID,
                "status": "paused",
            },
        )

        assert response.status_code == 404

    def test_update_session_summary(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test updating session summary."""
        session = session_storage.register(
            external_id="summary-update",
            machine_id="21000000-0000-4000-8000-000000000003",
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
                "session_id": UNKNOWN_SESSION_ID,
                "summary_path": "/path/to/summary.md",
            },
        )

        assert response.status_code == 404

    def test_list_sessions(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Test listing sessions."""
        # Create a few sessions
        session_storage.register(
            external_id="list-test-1",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.register(
            external_id="list-test-2",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="qwen",
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
        test_project: dict[str, Any],
    ) -> None:
        """Test listing sessions with query filters."""
        session_storage.register(
            external_id="filter-test-1",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.register(
            external_id="filter-test-2",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="qwen",
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
            database=session_storage.db,
            session_manager=None,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            bootstrap_config=BootstrapConfig(),
        )
        client = TestClient(server.app)
        response = client.get("/api/sessions")
        assert response.status_code == 503
        assert "Session manager not available" in response.json()["detail"]

    def test_register_without_manager(self, session_storage: SessionManager) -> None:
        """Test registering when session manager is None returns 503."""
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=None,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            bootstrap_config=BootstrapConfig(),
        )
        client = TestClient(server.app)
        response = client.post(
            "/api/sessions/register",
            json={
                "external_id": "test",
                "machine_id": "21000000-0000-4000-8000-000000000002",
                "source": "claude",
            },
        )
        assert response.status_code == 503

    def test_find_by_terminal_context_resolves_by_project_and_parent_pid(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Find the one active session matching project and parent PID."""
        session = session_storage.register(
            external_id="terminal-match",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 4242},
        )

        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={"project_id": test_project["id"], "parent_pid": 4242},
        )

        assert response.status_code == 200
        assert response.json()["session"]["id"] == session.id

    def test_find_by_terminal_context_rejects_multiple_active_matches(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Multiple active sessions for the same project and PID are ambiguous."""
        for external_id in ("terminal-ambiguous-1", "terminal-ambiguous-2"):
            session_storage.register(
                external_id=external_id,
                machine_id="21000000-0000-4000-8000-000000000003",
                source="codex",
                project_id=test_project["id"],
                terminal_context={"parent_pid": 4242},
            )

        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={"project_id": test_project["id"], "parent_pid": 4242},
        )

        assert response.status_code == 200
        assert response.json()["session"] is None

    def test_find_by_terminal_context_uses_terminal_context_to_disambiguate(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Optional terminal context narrows sessions sharing one parent PID."""
        session_storage.register(
            external_id="terminal-context-miss",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 4242, "tmux_pane": "%1"},
        )
        matched_session = session_storage.register(
            external_id="terminal-context-match",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 4242, "tmux_pane": "%2"},
        )

        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={
                "project_id": test_project["id"],
                "parent_pid": 4242,
                "terminal_context": {"tmux_pane": "%2"},
            },
        )

        assert response.status_code == 200
        assert response.json()["session"]["id"] == matched_session.id

    def test_find_by_terminal_context_ignores_other_projects_and_inactive_sessions(
        self,
        client: TestClient,
        session_storage: SessionManager,
        project_storage: LocalProjectManager,
        test_project: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Lookup only considers active sessions in the requested project."""
        other_project = project_storage.create(
            name="other-terminal-project",
            repo_path=str(tmp_path / "other"),
        )
        session_storage.register(
            external_id="terminal-other-project",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=other_project.id,
            terminal_context={"parent_pid": 4242},
        )
        inactive_session = session_storage.register(
            external_id="terminal-inactive",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="codex",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 4242},
        )
        session_storage.update_status(inactive_session.id, "expired")

        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={"project_id": test_project["id"], "parent_pid": 4242},
        )

        assert response.status_code == 200
        assert response.json()["session"] is None

    @pytest.mark.parametrize("parent_pid", [0, -1, True])
    def test_find_by_terminal_context_rejects_non_positive_parent_pid(
        self,
        client: TestClient,
        test_project: dict[str, Any],
        parent_pid: object,
    ) -> None:
        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={"project_id": test_project["id"], "parent_pid": parent_pid},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "parent_pid must be a positive integer"

    def test_find_by_terminal_context_matches_by_tmux_pane_when_pid_differs(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """Sessions can match by tmux_pane alone when stored parent_pid differs.

        This covers the case where ghook (hook command) and the MCP stdio proxy
        see different parent PIDs because the CLI spawns them through different
        process trees. The caller still sends a positive parent_pid, but the
        stored session's parent_pid may not match — terminal-context fields
        like tmux_pane provide the fallback identity.
        """
        session = session_storage.register(
            external_id="grok-pid-mismatch",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="grok",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 44483, "tmux_pane": "%51"},
        )

        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={
                "project_id": test_project["id"],
                "parent_pid": 99999,
                "terminal_context": {"tmux_pane": "%51"},
            },
        )

        assert response.status_code == 200
        assert response.json()["session"]["id"] == session.id

    def test_find_by_terminal_context_no_match_when_pid_and_context_both_mismatch(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """No session is returned when parent_pid differs and no context overlaps."""
        session_storage.register(
            external_id="grok-no-overlap",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="grok",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 44483, "tmux_pane": "%51"},
        )

        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={
                "project_id": test_project["id"],
                "parent_pid": 99999,
                "terminal_context": {"tmux_pane": "%99"},
            },
        )

        assert response.status_code == 200
        assert response.json()["session"] is None

    def test_find_by_terminal_context_prefers_pid_match_over_context_only(
        self,
        client: TestClient,
        session_storage: SessionManager,
        test_project: dict[str, Any],
    ) -> None:
        """A pid+pane match wins over a pane-only match."""
        pid_match_session = session_storage.register(
            external_id="grok-pid-match",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="grok",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 4242, "tmux_pane": "%10"},
        )
        session_storage.register(
            external_id="grok-pane-only",
            machine_id="21000000-0000-4000-8000-000000000003",
            source="grok",
            project_id=test_project["id"],
            terminal_context={"parent_pid": 55555, "tmux_pane": "%10"},
        )

        response = client.post(
            "/api/sessions/find_by_terminal_context",
            json={
                "project_id": test_project["id"],
                "parent_pid": 4242,
                "terminal_context": {"tmux_pane": "%10"},
            },
        )

        assert response.status_code == 200
        assert response.json()["session"]["id"] == pid_match_session.id

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
            json={"session_id": "21000000-0000-4000-8000-00000000001c"},  # missing status
        )
        assert response.status_code == 400

    def test_update_summary_missing_fields(self, client: TestClient) -> None:
        """Test update_summary with missing required fields."""
        response = client.post(
            "/api/sessions/update_summary",
            json={"session_id": "21000000-0000-4000-8000-00000000001c"},  # missing summary_path
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
        self.event_handlers = MagicMock()

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
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            mcp_manager=None,
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            bootstrap_config=BootstrapConfig(),
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
        del client.app.state.hook_manager
        response = client.post("/api/sessions/test-session/stop")
        assert response.status_code == 503
        assert "Hook manager not available" in response.json()["detail"]

    def test_stop_signal_without_stop_registry(self, session_storage: SessionManager) -> None:
        """Test stop signal endpoints when stop registry not available."""
        services = ServiceContainer(
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            bootstrap_config=BootstrapConfig(),
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
