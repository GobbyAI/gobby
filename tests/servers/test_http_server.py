"""Tests for the HTTP server endpoints."""

from collections.abc import Iterator
from datetime import UTC
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.servers.models import SessionRegisterRequest
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def session_storage(temp_db: LocalDatabase) -> LocalSessionManager:
    """Create session storage."""
    return LocalSessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: LocalDatabase) -> LocalProjectManager:
    """Create project storage."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def test_project(project_storage: LocalProjectManager, temp_dir: Path) -> dict:
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
    services = ServiceContainer(
        config=None,
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
def client(http_server: HTTPServer) -> Iterator[TestClient]:
    """Create a test client for the HTTP server."""
    with patch("gobby.servers.http.HookManager") as MockHM:
        mock_instance = MockHM.return_value
        mock_instance._stop_registry = MagicMock()
        mock_instance.shutdown = MagicMock()
        with TestClient(http_server.app) as client:
            yield client


class TestSessionRegisterRequest:
    """Tests for SessionRegisterRequest model."""

    def test_required_fields(self) -> None:
        """Test that external_id is required."""
        request = SessionRegisterRequest(
            external_id="test-key",
            machine_id=None,
            jsonl_path=None,
            title=None,
            source=None,
            parent_session_id=None,
            status=None,
            project_id=None,
            project_path=None,
            git_branch=None,
            cwd=None,
        )
        assert request.external_id == "test-key"

    def test_optional_fields(self) -> None:
        """Test all optional fields."""
        request = SessionRegisterRequest(
            external_id="test-key",
            machine_id="machine-123",
            jsonl_path="/path/to/transcript.jsonl",
            title="Test Session",
            source="Claude Code",
            parent_session_id="parent-uuid",
            status="active",
            project_id="project-uuid",
            project_path="/path/to/project",
            git_branch="main",
            cwd="/current/working/dir",
        )

        assert request.machine_id == "machine-123"
        assert request.title == "Test Session"
        assert request.git_branch == "main"


class TestAdminEndpoints:
    """Tests for admin endpoints."""

    def test_status_check(self, client: TestClient) -> None:
        """Test /admin/status endpoint returns health info."""
        response = client.get("/api/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert "status" in data
        assert "server" in data
        assert "port" in data["server"]
        assert data["server"]["test_mode"] is True

    def test_config_endpoint(self, client: TestClient) -> None:
        """Test /admin/config endpoint returns configuration."""
        response = client.get("/api/admin/config")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "success"
        assert "config" in data
        assert "server" in data["config"]
        assert "endpoints" in data["config"]

    def test_metrics_endpoint(self, client: TestClient) -> None:
        """Test /admin/metrics endpoint returns Prometheus format."""
        response = client.get("/api/admin/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")


class TestSessionEndpoints:
    """Tests for session endpoints."""

    def test_register_session(
        self,
        client: TestClient,
        test_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test session registration endpoint."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine"):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "test-cli-key",
                    "source": "claude",
                    "cwd": str(temp_dir),
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "registered"
        assert data["external_id"] == "test-cli-key"
        assert "id" in data

    def test_register_session_with_all_fields(
        self,
        client: TestClient,
        test_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test session registration with all optional fields."""
        response = client.post(
            "/api/sessions/register",
            json={
                "external_id": "full-cli-key",
                "machine_id": "custom-machine",
                "source": "Claude Code",
                "project_id": test_project["id"],
                "title": "Full Session",
                "jsonl_path": "/path/to/transcript.jsonl",
                "git_branch": "feature/test",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["external_id"] == "full-cli-key"
        assert data["machine_id"] == "custom-machine"

    def test_get_session(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
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

    def test_find_current_session(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
        test_project: dict,
    ) -> None:
        """Test finding current session by composite key."""
        session = session_storage.register(
            external_id="find-current",
            machine_id="my-machine",
            source="gemini",
            project_id=test_project["id"],
        )

        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "find-current",
                "machine_id": "my-machine",
                "source": "gemini",
                "project_id": test_project["id"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == session.id

    def test_find_current_session_not_found(self, client: TestClient, test_project: dict) -> None:
        """Test finding nonexistent current session."""
        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "nonexistent",
                "machine_id": "machine",
                "source": "claude",
                "project_id": test_project["id"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"] is None

    def test_find_current_session_missing_fields(self, client: TestClient) -> None:
        """Test find_current with missing required fields."""
        response = client.post(
            "/api/sessions/find_current",
            json={"external_id": "test"},
        )

        assert response.status_code == 400

    def test_find_current_session_missing_project_id(self, client: TestClient) -> None:
        """Test find_current without project_id or cwd returns 400."""
        response = client.post(
            "/api/sessions/find_current",
            json={
                "external_id": "test",
                "machine_id": "machine",
                "source": "claude",
            },
        )

        assert response.status_code == 400
        assert "project_id or cwd" in response.json()["detail"]

    def test_find_parent_session(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
        test_project: dict,
    ) -> None:
        """Test finding parent session for handoff."""
        session = session_storage.register(
            external_id="parent-session",
            machine_id="handoff-machine",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.update_status(session.id, "handoff_ready")

        response = client.post(
            "/api/sessions/find_parent",
            json={
                "machine_id": "handoff-machine",
                "source": "claude",
                "project_id": test_project["id"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == session.id

    def test_update_session_status(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
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
        session_storage: LocalSessionManager,
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
        session_storage: LocalSessionManager,
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
        session_storage: LocalSessionManager,
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
        """Test getting messages when message manager not available."""
        response = client.get("/api/sessions/test-session/messages")
        assert response.status_code == 503
        assert "Message manager not available" in response.json()["detail"]

    def test_list_sessions_without_manager(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test listing sessions when session manager is None returns 503."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=None,  # type: ignore
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

    def test_register_without_manager(self, session_storage: LocalSessionManager) -> None:
        """Test registering when session manager is None returns 503."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=None,  # type: ignore
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

    def test_find_parent_with_cwd(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
        test_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test finding parent session using cwd instead of project_id."""
        session = session_storage.register(
            external_id="parent-cwd-test",
            machine_id="cwd-machine",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.update_status(session.id, "handoff_ready")

        with patch("gobby.utils.machine_id.get_machine_id", return_value="cwd-machine"):
            response = client.post(
                "/api/sessions/find_parent",
                json={
                    "source": "claude",
                    "cwd": str(temp_dir),  # Use cwd instead of project_id
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == session.id

    def test_find_parent_missing_source(self, client: TestClient) -> None:
        """Test find_parent with missing source field."""
        response = client.post(
            "/api/sessions/find_parent",
            json={"machine_id": "test-machine"},
        )

        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_find_parent_missing_project_and_cwd(
        self,
        client: TestClient,
    ) -> None:
        """Test find_parent without project_id or cwd returns 400."""
        response = client.post(
            "/api/sessions/find_parent",
            json={
                "source": "claude",
                "machine_id": "test-machine",
            },
        )

        assert response.status_code == 400
        assert "project_id or cwd" in response.json()["detail"]

    def test_find_parent_no_session(
        self,
        client: TestClient,
        test_project: dict,
    ) -> None:
        """Test find_parent when no parent session exists."""
        response = client.post(
            "/api/sessions/find_parent",
            json={
                "source": "claude",
                "machine_id": "test-machine",
                "project_id": test_project["id"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"] is None

    def test_register_then_get_persistence(
        self,
        client: TestClient,
        test_project: dict,
        temp_dir: Path,
    ) -> None:
        """Test that session registered via HTTP is retrievable via get endpoint."""
        # Register via HTTP endpoint
        with patch("gobby.utils.machine_id.get_machine_id", return_value="persist-machine"):
            register_response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "persist-test-key",
                    "source": "claude",
                    "cwd": str(temp_dir),
                    "title": "Persistence Test Session",
                },
            )

        assert register_response.status_code == 200
        register_data = register_response.json()
        session_id = register_data["id"]

        # Retrieve via GET endpoint
        get_response = client.get(f"/api/sessions/{session_id}")
        assert get_response.status_code == 200

        get_data = get_response.json()
        assert get_data["status"] == "success"
        assert get_data["session"]["external_id"] == "persist-test-key"
        assert get_data["session"]["id"] == session_id
        assert get_data["session"]["title"] == "Persistence Test Session"

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

    def test_register_with_invalid_project_path(
        self,
        client: TestClient,
        temp_dir: Path,
    ) -> None:
        """Test registration with non-existent project path returns 400.

        When cwd points to a path without .gobby/project.json, _resolve_project_id
        raises ValueError, which is caught and converted to HTTP 400 Bad Request.
        """
        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine"):
            response = client.post(
                "/api/sessions/register",
                json={
                    "external_id": "invalid-path-test",
                    "source": "claude",
                    "cwd": "/nonexistent/path/that/does/not/exist",
                },
            )
        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "No .gobby/project.json found" in data["detail"]


class TestHooksEndpoint:
    """Tests for hooks execution endpoint."""

    def test_execute_hook_missing_hook_type(self, client: TestClient) -> None:
        """Test hook execution with missing hook_type."""
        response = client.post(
            "/api/hooks/execute",
            json={"source": "claude"},
        )

        assert response.status_code == 400
        assert "hook_type" in response.json()["detail"]

    def test_execute_hook_missing_source(self, client: TestClient) -> None:
        """Test hook execution with missing source."""
        response = client.post(
            "/api/hooks/execute",
            json={"hook_type": "session-start"},
        )

        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_execute_hook_unsupported_source(self, client: TestClient) -> None:
        """Test hook execution with unsupported source returns error."""
        response = client.post(
            "/api/hooks/execute",
            json={
                "hook_type": "session-start",
                "source": "unsupported",
            },
        )

        # In test mode, HookManager may not be initialized (503) or source is invalid (400)
        assert response.status_code in [400, 503]


class TestMCPEndpoints:
    """Tests for MCP proxy endpoints."""

    def test_mcp_tools_without_manager(self, client: TestClient) -> None:
        """Test MCP tools listing when manager not available."""
        response = client.get("/api/mcp/test-server/tools")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "MCP manager not available" in data["error"]

    def test_mcp_proxy_without_manager(self, client: TestClient) -> None:
        """Test MCP proxy when manager not available."""
        response = client.post(
            "/api/mcp/test-server/tools/test-tool",
            json={},
        )
        assert response.status_code == 503


class FakeConnection:
    def __init__(self) -> None:
        self.is_connected = True
        self._session = MagicMock()
        self.config = MagicMock()
        self.config.transport = "stdio"
        self.config.project_id = "test-project"
        self.config.description = "Test Server"


class FakeMCPManager:
    def __init__(self) -> None:
        self.server_configs: list[Any] = []
        self.connections: dict[str, Any] = {}
        self.health: dict[str, Any] = {}
        self.get_client = MagicMock()
        self.call_tool = AsyncMock()
        self.project_id = "test-project"
        self.mcp_db_manager = None

    def has_server(self, server_name: str) -> bool:
        """Check if a server is configured."""
        return server_name in self.connections


class TestMCPEndpointsWithManager:
    """Tests for MCP endpoints with mock manager."""

    @pytest.fixture
    def mock_mcp_manager(self) -> FakeMCPManager:
        """Create a mock MCP manager."""

        return FakeMCPManager()

    @pytest.fixture
    def http_server_with_mcp(
        self,
        session_storage: LocalSessionManager,
        mock_mcp_manager: FakeMCPManager,
    ) -> HTTPServer:
        """Create HTTP server with mock MCP manager."""
        services = ServiceContainer(
            config=None,
            database=session_storage.db,
            session_manager=session_storage,
            task_manager=MagicMock(),
            mcp_manager=mock_mcp_manager,
        )
        return HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
        )

    @pytest.fixture
    def mcp_client(self, http_server_with_mcp: HTTPServer) -> Iterator[TestClient]:
        """Create test client with MCP manager."""
        with TestClient(http_server_with_mcp.app) as client:
            yield client

    def test_mcp_tools_server_not_found(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test MCP tools listing for unknown server."""
        assert http_server_with_mcp.mcp_manager is not None
        http_server_with_mcp.mcp_manager.get_client.side_effect = ValueError("Server not found")

        # No try/except needed if we fixed the root cause, but leaving assertion
        response = mcp_client.get("/api/mcp/unknown-server/tools")
        assert response.status_code == 404

    def test_mcp_proxy_tool_not_found(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test MCP proxy for unknown tool.

        Tool-level errors (tool not found, validation, execution) are returned as
        200 with error in response body. Only server-level errors return 404.
        See _process_tool_proxy_result in routes/mcp/tools.py.
        """
        assert http_server_with_mcp.mcp_manager is not None
        http_server_with_mcp.mcp_manager.call_tool = AsyncMock(
            side_effect=ValueError("Tool not found")
        )

        response = mcp_client.post(
            "/api/mcp/test-server/tools/unknown-tool",
            json={},
        )
        # Tool-level errors return 200 with error in body (application-level error)
        assert response.status_code == 200
        data = response.json()
        assert data.get("success") is False  # Wrapper propagates inner result success
        result = data.get("result", {})
        assert result.get("success") is False  # Inner error from ToolProxyService
        assert "Tool not found" in result.get("error", "")

    def test_add_mcp_server_success(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test adding a new MCP server."""
        # Mock get_project_context
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "test-project-id", "name": "test"}
            assert http_server_with_mcp.mcp_manager is not None
            http_server_with_mcp.mcp_manager.add_server = AsyncMock()

            response = mcp_client.post(
                "/api/mcp/servers",
                json={
                    "name": "new-server",
                    "transport": "http",
                    "url": "http://example.com",
                    "enabled": True,
                },
            )

        assert response.status_code == 200
        assert response.json()["success"] is True

        # Verify add_server was called with correct config
        assert http_server_with_mcp.mcp_manager is not None
        http_server_with_mcp.mcp_manager.add_server.assert_called_once()
        config = http_server_with_mcp.mcp_manager.add_server.call_args[0][0]
        assert config.name == "new-server"
        assert config.project_id == "test-project-id"

    def test_add_mcp_server_no_project(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ) -> None:
        """Test adding MCP server without project context fails."""
        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            response = mcp_client.post(
                "/api/mcp/servers",
                json={
                    "name": "new-server",
                    "transport": "http",
                    "url": "http://example.com",
                },
            )

        assert response.status_code == 400
        # HTTPException returns {"success": False, "error": "..."} in detail
        detail = response.json()["detail"]
        error_msg = detail.get("error", "") if isinstance(detail, dict) else str(detail)
        assert "No current project" in error_msg


class TestExceptionHandling:
    """Tests for exception handling."""

    def test_global_exception_returns_200(
        self,
        session_storage: LocalSessionManager,
    ) -> None:
        """Test that global exception handler returns 200 to prevent hook failures."""
        # Create server that will raise an exception
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

        # Mock to raise exception
        # Call an endpoint that uses session_storage.get (e.g. invalid status update which fetches session)
        # but here we mocked .get globally.
        # Let's use a simpler approach: define a route in the app that raises an exception
        @server.app.get("/trigger_error")
        def trigger_error() -> None:
            raise RuntimeError("Test error")

        client = TestClient(server.app, raise_server_exceptions=False)
        response = client.get("/trigger_error")

        # Should return 200 with error details in JSON (as per global handler logic for hooks/background)
        # OR 500 if it's a standard request.
        # Wait, the requirement says "verify global exception handler".
        # If the handler is for hooks, it traps exceptions.
        # If for standard HTTP, it likely returns 500.
        # Let's check what the global handler actually does.
        # Assuming it traps and logs, allowing the server to stay alive.

        # For this test, let's assume standard behavior (500) but ensuring app doesn't crash on outer loop.
        # Actually, if it's 500, that IS handled. Unhandled would crash uvicorn worker.
        # The global exception handler traps errors and returns 200 with error details
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "error"
        assert "message" in data


class TestShutdownEndpoint:
    """Tests for shutdown endpoint."""

    def test_shutdown_initiates(self, client: TestClient) -> None:
        """Test that shutdown endpoint initiates shutdown."""
        response = client.post("/api/admin/shutdown")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "shutting_down"
        assert "response_time_ms" in data


class FakeStopSignal:
    """Fake stop signal for testing."""

    def __init__(
        self,
        signal_id: str = "sig-123",
        reason: str = "Test stop",
        source: str = "http_api",
    ) -> None:
        from datetime import datetime

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

    def shutdown(self) -> None:
        pass


class TestStopSignalEndpoints:
    """Tests for stop signal HTTP endpoints."""

    @pytest.fixture
    def server_with_stop_registry(
        self,
        session_storage: LocalSessionManager,
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
        assert "signal_id" in data
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
        assert "signal_id" in data
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

    def test_stop_signal_without_stop_registry(self, session_storage: LocalSessionManager) -> None:
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
            mock_hm = MagicMock()
            mock_hm._stop_registry = None
            client.app.state.hook_manager = mock_hm

            response = client.post("/api/sessions/test-session/stop")

        assert response.status_code == 503
        assert "Stop registry not available" in response.json()["detail"]
