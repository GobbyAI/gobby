"""Tests for project API routes with real database objects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from tests.servers.conftest import create_http_server

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import LocalSessionManager

pytestmark = pytest.mark.unit


class TestProjectRoutes:
    """Tests for project management endpoints using real DB objects."""

    @pytest.fixture
    def client(
        self, session_manager: LocalSessionManager, project_manager: LocalProjectManager
    ) -> TestClient:
        """Create a test client with real session_manager and project_manager."""
        server = create_http_server(
            session_manager=session_manager,
            database=session_manager.db,
        )
        return TestClient(server.app)

    @pytest.fixture
    def real_project(self, project_manager: LocalProjectManager) -> dict:
        """Create a real project in the database."""
        proj = project_manager.create(
            name="my-project",
            repo_path="/tmp/my-project",
            github_url="https://github.com/test/my-project",
        )
        return proj.to_dict()

    @pytest.fixture
    def personal_project(self, project_manager: LocalProjectManager) -> dict:
        """Get the _personal system project (created by migrations)."""
        proj = project_manager.get_by_name("_personal")
        assert proj is not None, "_personal should be created by migrations"
        return proj.to_dict()

    @pytest.fixture
    def orphaned_project(self, project_manager: LocalProjectManager) -> dict:
        """Get or create the _orphaned hidden project."""
        proj = project_manager.get_or_create(name="_orphaned", repo_path=None)
        return proj.to_dict()

    @pytest.fixture
    def migrated_project(self, project_manager: LocalProjectManager) -> dict:
        """Get or create the _migrated hidden project."""
        proj = project_manager.get_or_create(name="_migrated", repo_path=None)
        return proj.to_dict()

    # -----------------------------------------------------------------
    # GET /api/projects (list)
    # -----------------------------------------------------------------

    def test_list_projects_default(self, client: TestClient) -> None:
        """List returns only default _personal project (shown as Personal)."""
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Migrations create _personal by default; it's shown (not hidden)
        names = [p["name"] for p in data]
        assert "_orphaned" not in names
        assert "_migrated" not in names

    def test_list_projects_with_project(self, client: TestClient, real_project: dict) -> None:
        """List returns created project with display_name and stats."""
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        # _personal is always created by migrations, so we expect at least 2
        proj = next(p for p in data if p["id"] == real_project["id"])
        assert proj["name"] == "my-project"
        assert proj["display_name"] == "my-project"
        assert "session_count" in proj
        assert "open_task_count" in proj
        assert "last_activity_at" in proj

    def test_list_projects_personal_display_name(
        self, client: TestClient, personal_project: dict
    ) -> None:
        """_personal project shows display_name = 'Personal'."""
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        personal = [p for p in data if p["name"] == "_personal"]
        assert len(personal) == 1
        assert personal[0]["display_name"] == "Personal"

    def test_list_projects_hides_orphaned(
        self, client: TestClient, orphaned_project: dict, real_project: dict
    ) -> None:
        """_orphaned project is hidden from the list."""
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        names = [p["name"] for p in data]
        assert "_orphaned" not in names
        assert "my-project" in names

    def test_list_projects_hides_migrated(
        self, client: TestClient, migrated_project: dict, real_project: dict
    ) -> None:
        """_migrated project is hidden from the list."""
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        names = [p["name"] for p in data]
        assert "_migrated" not in names

    def test_list_projects_stats_with_sessions(
        self,
        client: TestClient,
        real_project: dict,
        session_manager: LocalSessionManager,
    ) -> None:
        """Project stats reflect actual session and task counts."""
        # Create a session for this project using register()
        session_manager.register(
            external_id="ext-100",
            source="claude",
            machine_id="test-machine",
            project_id=real_project["id"],
        )
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        proj = next(p for p in data if p["id"] == real_project["id"])
        assert proj["session_count"] == 1

    # -----------------------------------------------------------------
    # GET /api/projects/{project_id}
    # -----------------------------------------------------------------

    def test_get_project(self, client: TestClient, real_project: dict) -> None:
        """Get a specific project by ID."""
        response = client.get(f"/api/projects/{real_project['id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == real_project["id"]
        assert data["name"] == "my-project"
        assert data["display_name"] == "my-project"
        assert "session_count" in data

    def test_get_project_personal_display_name(
        self, client: TestClient, personal_project: dict
    ) -> None:
        """Get _personal project shows 'Personal' as display_name."""
        response = client.get(f"/api/projects/{personal_project['id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["display_name"] == "Personal"

    def test_get_project_not_found(self, client: TestClient) -> None:
        """404 when project doesn't exist."""
        response = client.get("/api/projects/nonexistent-id")
        assert response.status_code == 404

    def test_get_project_soft_deleted(
        self, client: TestClient, real_project: dict, project_manager: LocalProjectManager
    ) -> None:
        """404 when project is soft-deleted."""
        project_manager.soft_delete(real_project["id"])
        response = client.get(f"/api/projects/{real_project['id']}")
        assert response.status_code == 404

    # -----------------------------------------------------------------
    # PUT /api/projects/{project_id}
    # -----------------------------------------------------------------

    def test_update_project_name(self, client: TestClient, real_project: dict) -> None:
        """Update project name."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"name": "new-name"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "new-name"
        assert data["display_name"] == "new-name"

    def test_update_project_github_url(self, client: TestClient, real_project: dict) -> None:
        """Update project github_url."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"github_url": "https://github.com/test/updated"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["github_url"] == "https://github.com/test/updated"

    def test_update_project_repo_path(self, client: TestClient, real_project: dict) -> None:
        """Update project repo_path."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"repo_path": "/new/path"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["repo_path"] == "/new/path"

    def test_update_project_github_repo(self, client: TestClient, real_project: dict) -> None:
        """Update project github_repo field."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"github_repo": "owner/repo"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["github_repo"] == "owner/repo"

    def test_update_project_linear_team_id(self, client: TestClient, real_project: dict) -> None:
        """Update project linear_team_id field."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"linear_team_id": "TEAM-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["linear_team_id"] == "TEAM-123"

    def test_update_project_empty_body(self, client: TestClient, real_project: dict) -> None:
        """Empty update body returns current project data unchanged."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "my-project"

    def test_update_project_not_found(self, client: TestClient) -> None:
        """404 when updating nonexistent project."""
        response = client.put(
            "/api/projects/nonexistent-id",
            json={"name": "new-name"},
        )
        assert response.status_code == 404

    def test_update_project_soft_deleted(
        self, client: TestClient, real_project: dict, project_manager: LocalProjectManager
    ) -> None:
        """404 when updating soft-deleted project."""
        project_manager.soft_delete(real_project["id"])
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"name": "new-name"},
        )
        assert response.status_code == 404

    def test_update_personal_project_display_name(
        self, client: TestClient, personal_project: dict
    ) -> None:
        """Updating _personal project keeps display_name as Personal if name stays."""
        # Update something other than name
        response = client.put(
            f"/api/projects/{personal_project['id']}",
            json={"repo_path": "/updated/path"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["display_name"] == "Personal"

    # -----------------------------------------------------------------
    # DELETE /api/projects/{project_id}
    # -----------------------------------------------------------------

    def test_delete_project(self, client: TestClient, real_project: dict) -> None:
        """Successfully soft-delete a project."""
        response = client.delete(f"/api/projects/{real_project['id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"
        assert data["id"] == real_project["id"]

    def test_delete_project_not_found(self, client: TestClient) -> None:
        """404 when deleting nonexistent project."""
        response = client.delete("/api/projects/nonexistent-id")
        assert response.status_code == 404

    def test_delete_project_already_deleted(
        self, client: TestClient, real_project: dict, project_manager: LocalProjectManager
    ) -> None:
        """404 when deleting already soft-deleted project."""
        project_manager.soft_delete(real_project["id"])
        response = client.delete(f"/api/projects/{real_project['id']}")
        assert response.status_code == 404

    def test_delete_protected_personal(self, client: TestClient, personal_project: dict) -> None:
        """Cannot delete _personal (system project)."""
        response = client.delete(f"/api/projects/{personal_project['id']}")
        assert response.status_code == 403

    def test_delete_protected_orphaned(self, client: TestClient, orphaned_project: dict) -> None:
        """Cannot delete _orphaned (system project)."""
        response = client.delete(f"/api/projects/{orphaned_project['id']}")
        assert response.status_code == 403

    def test_delete_protected_migrated(self, client: TestClient, migrated_project: dict) -> None:
        """Cannot delete _migrated (system project)."""
        response = client.delete(f"/api/projects/{migrated_project['id']}")
        assert response.status_code == 403

    # -----------------------------------------------------------------
    # Error: session_manager unavailable
    # -----------------------------------------------------------------

    def test_session_manager_unavailable(self, temp_db: LocalDatabase) -> None:
        """503 when session_manager is None."""
        server = create_http_server(session_manager=None, database=temp_db)
        client = TestClient(server.app)
        response = client.get("/api/projects")
        assert response.status_code == 503
