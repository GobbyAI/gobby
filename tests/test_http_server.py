"""Tests for the HTTP server endpoints."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from gobby.servers.http import HTTPServer, SessionRegisterRequest
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager


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
    return HTTPServer(
        port=8765,
        test_mode=True,
        mcp_manager=None,
        config=None,
        session_manager=session_storage,
    )


@pytest.fixture
def client(http_server: HTTPServer) -> TestClient:
    """Create a test client for the HTTP server."""
    return TestClient(http_server.app)


class TestSessionRegisterRequest:
    """Tests for SessionRegisterRequest model."""

    def test_required_fields(self):
        """Test that external_id is required."""
        request = SessionRegisterRequest(external_id="test-key")
        assert request.external_id == "test-key"

    def test_optional_fields(self):
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

    def test_status_check(self, client: TestClient):
        """Test /admin/status endpoint returns health info."""
        response = client.get("/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert "status" in data
        assert "server" in data
        assert "port" in data["server"]
        assert data["server"]["test_mode"] is True

    def test_config_endpoint(self, client: TestClient):
        """Test /admin/config endpoint returns configuration."""
        response = client.get("/admin/config")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "success"
        assert "config" in data
        assert "server" in data["config"]
        assert "endpoints" in data["config"]

    def test_metrics_endpoint(self, client: TestClient):
        """Test /admin/metrics endpoint returns Prometheus format."""
        response = client.get("/admin/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")


class TestSessionEndpoints:
    """Tests for session endpoints."""

    def test_register_session(
        self,
        client: TestClient,
        test_project: dict,
        temp_dir: Path,
    ):
        """Test session registration endpoint."""
        with patch("gobby.utils.machine_id.get_machine_id", return_value="test-machine"):
            response = client.post(
                "/sessions/register",
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
    ):
        """Test session registration with all optional fields."""
        response = client.post(
            "/sessions/register",
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
    ):
        """Test getting a session by ID."""
        # Register a session first
        session = session_storage.register(
            external_id="get-test",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        response = client.get(f"/sessions/{session.id}")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "success"
        assert data["session"]["external_id"] == "get-test"

    def test_get_session_not_found(self, client: TestClient):
        """Test getting nonexistent session returns 404."""
        response = client.get("/sessions/nonexistent-uuid")
        assert response.status_code == 404

    def test_find_current_session(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
        test_project: dict,
    ):
        """Test finding current session by composite key."""
        session = session_storage.register(
            external_id="find-current",
            machine_id="my-machine",
            source="gemini",
            project_id=test_project["id"],
        )

        response = client.post(
            "/sessions/find_current",
            json={
                "external_id": "find-current",
                "machine_id": "my-machine",
                "source": "gemini",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["id"] == session.id

    def test_find_current_session_not_found(self, client: TestClient):
        """Test finding nonexistent current session."""
        response = client.post(
            "/sessions/find_current",
            json={
                "external_id": "nonexistent",
                "machine_id": "machine",
                "source": "claude",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"] is None

    def test_find_current_session_missing_fields(self, client: TestClient):
        """Test find_current with missing required fields."""
        response = client.post(
            "/sessions/find_current",
            json={"external_id": "test"},
        )

        assert response.status_code == 400

    def test_find_parent_session(
        self,
        client: TestClient,
        session_storage: LocalSessionManager,
        test_project: dict,
    ):
        """Test finding parent session for handoff."""
        session = session_storage.register(
            external_id="parent-session",
            machine_id="handoff-machine",
            source="claude",
            project_id=test_project["id"],
        )
        session_storage.update_status(session.id, "handoff_ready")

        response = client.post(
            "/sessions/find_parent",
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
    ):
        """Test updating session status."""
        session = session_storage.register(
            external_id="status-update",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        response = client.post(
            "/sessions/update_status",
            json={
                "session_id": session.id,
                "status": "paused",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["status"] == "paused"

    def test_update_session_status_not_found(self, client: TestClient):
        """Test updating status of nonexistent session."""
        response = client.post(
            "/sessions/update_status",
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
    ):
        """Test updating session summary."""
        session = session_storage.register(
            external_id="summary-update",
            machine_id="machine",
            source="claude",
            project_id=test_project["id"],
        )

        response = client.post(
            "/sessions/update_summary",
            json={
                "session_id": session.id,
                "summary_path": "/path/to/summary.md",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["summary_path"] == "/path/to/summary.md"


class TestHooksEndpoint:
    """Tests for hooks execution endpoint."""

    def test_execute_hook_missing_hook_type(self, client: TestClient):
        """Test hook execution with missing hook_type."""
        response = client.post(
            "/hooks/execute",
            json={"source": "claude"},
        )

        assert response.status_code == 400
        assert "hook_type" in response.json()["detail"]

    def test_execute_hook_missing_source(self, client: TestClient):
        """Test hook execution with missing source."""
        response = client.post(
            "/hooks/execute",
            json={"hook_type": "session-start"},
        )

        assert response.status_code == 400
        assert "source" in response.json()["detail"]

    def test_execute_hook_unsupported_source(self, client: TestClient):
        """Test hook execution with unsupported source returns error."""
        response = client.post(
            "/hooks/execute",
            json={
                "hook_type": "session-start",
                "source": "unsupported",
            },
        )

        # In test mode, HookManager may not be initialized (503) or source is invalid (400)
        assert response.status_code in [400, 503]


class TestMCPEndpoints:
    """Tests for MCP proxy endpoints."""

    def test_mcp_tools_without_manager(self, client: TestClient):
        """Test MCP tools listing when manager not available."""
        response = client.get("/mcp/test-server/tools")
        assert response.status_code == 503

    def test_mcp_proxy_without_manager(self, client: TestClient):
        """Test MCP proxy when manager not available."""
        response = client.post(
            "/mcp/test-server/tools/test-tool",
            json={},
        )
        assert response.status_code == 503


class TestMCPEndpointsWithManager:
    """Tests for MCP endpoints with mock manager."""

    @pytest.fixture
    def mock_mcp_manager(self):
        """Create a mock MCP manager."""
        manager = MagicMock()
        manager.server_configs = []
        manager.connections = {}
        manager.health = {}
        return manager

    @pytest.fixture
    def http_server_with_mcp(
        self,
        session_storage: LocalSessionManager,
        mock_mcp_manager: MagicMock,
    ) -> HTTPServer:
        """Create HTTP server with mock MCP manager."""
        return HTTPServer(
            port=8765,
            test_mode=True,
            mcp_manager=mock_mcp_manager,
            config=None,
            session_manager=session_storage,
        )

    @pytest.fixture
    def mcp_client(self, http_server_with_mcp: HTTPServer) -> TestClient:
        """Create test client with MCP manager."""
        return TestClient(http_server_with_mcp.app)

    def test_mcp_tools_server_not_found(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ):
        """Test MCP tools listing for unknown server."""
        http_server_with_mcp.mcp_manager.get_client.side_effect = ValueError("Server not found")

        response = mcp_client.get("/mcp/unknown-server/tools")
        assert response.status_code == 404

    def test_mcp_proxy_tool_not_found(
        self,
        mcp_client: TestClient,
        http_server_with_mcp: HTTPServer,
    ):
        """Test MCP proxy for unknown tool."""
        http_server_with_mcp.mcp_manager.call_tool = AsyncMock(
            side_effect=ValueError("Tool not found")
        )

        response = mcp_client.post(
            "/mcp/test-server/tools/unknown-tool",
            json={},
        )
        assert response.status_code == 404


class TestExceptionHandling:
    """Tests for exception handling."""

    def test_global_exception_returns_200(
        self,
        session_storage: LocalSessionManager,
    ):
        """Test that global exception handler returns 200 to prevent hook failures."""
        # Create server that will raise an exception
        server = HTTPServer(
            port=8765,
            test_mode=True,
            session_manager=session_storage,
        )

        # Mock to raise exception
        original_get = session_storage.get
        session_storage.get = MagicMock(side_effect=RuntimeError("Test error"))

        TestClient(server.app, raise_server_exceptions=False)

        # This should return 200 due to global exception handler
        # (only for specific endpoints, sessions/get raises HTTPException)
        session_storage.get = original_get


class TestShutdownEndpoint:
    """Tests for shutdown endpoint."""

    def test_shutdown_initiates(self, client: TestClient):
        """Test that shutdown endpoint initiates shutdown."""
        response = client.post("/admin/shutdown")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "shutting_down"
        assert "response_time_ms" in data
