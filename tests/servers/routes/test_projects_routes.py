"""Tests for project API routes with real database objects."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from gobby.projects.purge import PurgeOutcome
from gobby.servers.routes import projects as projects_routes
from gobby.storage.tasks import LocalTaskManager
from tests.servers.conftest import create_http_server

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestProjectRoutes:
    """Tests for project management endpoints using real DB objects."""

    @pytest.fixture
    def client(
        self, session_manager: SessionManager, project_manager: LocalProjectManager
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
        session_manager: SessionManager,
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

    @pytest.mark.parametrize("project_count", [1, 3])
    def test_list_projects_batches_stats_queries(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        session_manager: SessionManager,
        project_count: int,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        projects = [
            project_manager.create(name=f"batch-project-{index}", repo_path=None)
            for index in range(project_count)
        ]
        task_manager = LocalTaskManager(session_manager.db)
        for index, project in enumerate(projects):
            session_manager.register(
                external_id=f"batch-session-{index}",
                source="codex",
                machine_id="test-machine",
                project_id=project.id,
            )
            task_manager.create_task(project_id=project.id, title=f"Open task {index}")

        original_fetchall = session_manager.db.fetchall
        stats_queries = 0

        def counting_fetchall(sql: str, params: tuple = ()):
            nonlocal stats_queries
            if "GROUP BY project_id" in sql and ("FROM sessions" in sql or "FROM tasks" in sql):
                stats_queries += 1
            return original_fetchall(sql, params)

        monkeypatch.setattr(session_manager.db, "fetchall", counting_fetchall)

        response = client.get("/api/projects")

        assert response.status_code == 200
        payloads = {item["id"]: item for item in response.json()}
        assert stats_queries == 2
        for project in projects:
            assert payloads[project.id]["session_count"] == 1
            assert payloads[project.id]["open_task_count"] == 1
            assert payloads[project.id]["last_activity_at"] is not None

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
        assert data["created_at"] == real_project["created_at"]
        assert data["updated_at"] == real_project["updated_at"]
        assert data["display_name"] == "my-project"
        assert "session_count" in data
        assert data["approval_rules"] == []

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

    def test_update_project_clears_explicit_null_and_preserves_unset_fields(
        self, client: TestClient, real_project: dict
    ) -> None:
        """Explicit null clears a field while omitted fields remain unchanged."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"github_url": None},
        )
        assert response.status_code == 200
        assert response.json()["github_url"] is None

        response = client.get(f"/api/projects/{real_project['id']}")
        assert response.status_code == 200
        data = response.json()
        assert data["github_url"] is None
        assert data["name"] == "my-project"

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

    def test_update_project_linear_project_id(self, client: TestClient, real_project: dict) -> None:
        """Update project linear_project_id field."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={"linear_project_id": "LIN-PROJ"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["linear_project_id"] == "LIN-PROJ"

    def test_enable_linear_sync_requires_complete_binding(
        self, client: TestClient, real_project: dict
    ) -> None:
        response = client.patch(
            f"/api/projects/{real_project['id']}",
            json={"linear_sync_enabled": True},
        )

        assert response.status_code == 400
        assert "linear_team_id and linear_project_id" in response.json()["detail"]

    def test_enable_linear_sync_with_binding(self, client: TestClient, real_project: dict) -> None:
        response = client.patch(
            f"/api/projects/{real_project['id']}",
            json={
                "linear_team_id": "team-1",
                "linear_project_id": "linear-project-1",
                "linear_sync_enabled": True,
            },
        )

        assert response.status_code == 200
        assert response.json()["linear_sync_enabled"] is True

    def test_integrations_status_reports_live_counts_before_first_run(
        self,
        client: TestClient,
        real_project: dict,
        session_manager: SessionManager,
    ) -> None:
        task_manager = LocalTaskManager(session_manager.db)
        task_manager.create_task(project_id=real_project["id"], title="Pending")
        task_manager.create_task(
            project_id=real_project["id"],
            title="Linked",
            linear_issue_id="linear-1",
            github_repo="test/my-project",
            github_issue_number=1,
        )

        response = client.get(f"/api/projects/{real_project['id']}/integrations/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["linear"]["state"] == "pending"
        assert payload["linear"]["linked_count"] == 1
        assert payload["linear"]["pending_count"] == 1
        assert payload["github"]["linked_count"] == 1
        assert payload["github"]["pending_count"] == 0
        assert payload["github"]["readiness_error"] == "GitHub connector is unavailable"

    def test_update_project_empty_body(self, client: TestClient, real_project: dict) -> None:
        """Empty update body returns current project data unchanged."""
        response = client.put(
            f"/api/projects/{real_project['id']}",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "my-project"

    def test_update_project_approval_rules(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        project = project_manager.create(
            name="rules-project",
            repo_path=str(repo_path),
            github_url="https://github.com/test/rules-project",
        )

        response = client.put(
            f"/api/projects/{project.id}",
            json={"approval_rules": ["tool:Write", " tool:Write ", "mcp:third-party:*"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["approval_rules"] == ["tool:Write", "mcp:third-party:*"]

        project_file = repo_path / ".gobby" / "project.json"
        assert project_file.exists()
        saved = json.loads(project_file.read_text())
        assert saved["tool_approvals"]["allow"] == ["tool:Write", "mcp:third-party:*"]

    def test_update_project_approval_rules_requires_repo_path(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
    ) -> None:
        project = project_manager.create(name="no-repo-project", repo_path=None)

        response = client.put(
            f"/api/projects/{project.id}",
            json={"approval_rules": ["tool:Write"]},
        )
        assert response.status_code == 400

    def test_update_project_validation_detection(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        project = project_manager.create(
            name="validation-detection-project",
            repo_path=str(repo_path),
            github_url="https://github.com/test/validation-detection-project",
        )

        payload = {
            "builtin_matchers_enabled": False,
            "custom_matchers": [
                {
                    "id": "project-ci",
                    "label": "Project CI",
                    "prefixes": ["./scripts/ci"],
                }
            ],
        }
        response = client.put(
            f"/api/projects/{project.id}",
            json={"validation_detection": payload},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["validation_detection"]["builtin_matchers_enabled"] is False
        project_file = repo_path / ".gobby" / "project.json"
        saved = json.loads(project_file.read_text())
        assert saved["validation_detection"]["custom_matchers"][0]["id"] == "project-ci"

    def test_update_project_invalid_validation_detection_does_not_mutate(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old_repo = tmp_path / "old-repo"
        new_repo = tmp_path / "new-repo"
        old_repo.mkdir()
        new_repo.mkdir()
        project = project_manager.create(name="unchanged", repo_path=str(old_repo))
        project_file = old_repo / ".gobby" / "project.json"
        project_file.parent.mkdir(parents=True)
        original_payload = {"verification": {"lint": "uv run ruff check src/"}}
        project_file.write_text(json.dumps(original_payload))

        def fail_mutation(*args: object, **kwargs: object) -> None:
            raise AssertionError("invalid request attempted a project-file mutation")

        for function_name in (
            "save_project_approval_rules",
            "migrate_project_approval_rules",
            "clear_project_approval_rules",
            "save_project_validation_detection",
            "clear_project_validation_detection",
        ):
            monkeypatch.setattr(projects_routes, function_name, fail_mutation)

        response = client.put(
            f"/api/projects/{project.id}",
            json={
                "name": "changed",
                "repo_path": str(new_repo),
                "approval_rules": ["tool:Write"],
                "validation_detection": {
                    "custom_matchers": [{"id": " ", "label": "Invalid", "prefixes": ["test"]}]
                },
            },
        )

        assert response.status_code == 400
        persisted = project_manager.get(project.id)
        assert persisted is not None
        assert persisted.name == "unchanged"
        assert persisted.repo_path == str(old_repo)
        assert json.loads(project_file.read_text()) == original_payload
        assert not (new_repo / ".gobby" / "project.json").exists()

    def test_update_project_file_failure_rolls_back_database_update(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        project = project_manager.create(name="unchanged", repo_path=str(repo_path))

        def fail_save(*args: object, **kwargs: object) -> None:
            raise OSError("project file write failed")

        monkeypatch.setattr(projects_routes, "save_project_validation_detection", fail_save)

        with pytest.raises(OSError, match="project file write failed"):
            client.put(
                f"/api/projects/{project.id}",
                json={"name": "changed", "validation_detection": {}},
            )

        persisted = project_manager.get(project.id)
        assert persisted is not None
        assert persisted.name == "unchanged"

    def test_update_project_repo_path_migrates_approval_rules_and_preserves_metadata(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        old_repo = tmp_path / "old-repo"
        new_repo = tmp_path / "new-repo"
        old_repo.mkdir()
        new_repo.mkdir()
        project = project_manager.create(name="migrate-project", repo_path=str(old_repo))
        project_file = old_repo / ".gobby" / "project.json"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text(
            json.dumps(
                {
                    "id": project.id,
                    "name": project.name,
                    "created_at": project.created_at.isoformat(),
                    "verification": {"lint": "uv run ruff check src/"},
                    "tool_approvals": {"allow": ["tool:Write"]},
                }
            )
        )

        response = client.put(
            f"/api/projects/{project.id}",
            json={"repo_path": str(new_repo)},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["repo_path"] == str(new_repo)
        assert data["approval_rules"] == ["tool:Write"]

        migrated = json.loads((new_repo / ".gobby" / "project.json").read_text())
        assert migrated["id"] == project.id
        assert migrated["verification"]["lint"] == "uv run ruff check src/"
        assert migrated["tool_approvals"]["allow"] == ["tool:Write"]

        original = json.loads(project_file.read_text())
        assert original["verification"]["lint"] == "uv run ruff check src/"
        assert "tool_approvals" not in original

    def test_update_project_repo_path_with_explicit_approval_rules_preserves_metadata(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        old_repo = tmp_path / "old-repo"
        new_repo = tmp_path / "new-repo"
        old_repo.mkdir()
        new_repo.mkdir()
        project = project_manager.create(name="migrate-project", repo_path=str(old_repo))
        project_file = old_repo / ".gobby" / "project.json"
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text(
            json.dumps(
                {
                    "id": project.id,
                    "name": project.name,
                    "created_at": project.created_at.isoformat(),
                    "verification": {"unit_tests": "uv run pytest tests/ -v"},
                    "tool_approvals": {"allow": ["tool:Write"]},
                }
            )
        )

        response = client.put(
            f"/api/projects/{project.id}",
            json={"repo_path": str(new_repo), "approval_rules": ["mcp:third-party:*"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["repo_path"] == str(new_repo)
        assert data["approval_rules"] == ["mcp:third-party:*"]

        migrated = json.loads((new_repo / ".gobby" / "project.json").read_text())
        assert migrated["id"] == project.id
        assert migrated["verification"]["unit_tests"] == "uv run pytest tests/ -v"
        assert migrated["tool_approvals"]["allow"] == ["mcp:third-party:*"]

        original = json.loads(project_file.read_text())
        assert original["verification"]["unit_tests"] == "uv run pytest tests/ -v"
        assert "tool_approvals" not in original

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

    def test_purge_project_uses_runner_service(
        self,
        session_manager: SessionManager,
        real_project: dict,
    ) -> None:
        calls: list[str] = []

        class PurgeService:
            async def purge_project(self, project_id: str) -> PurgeOutcome:
                calls.append(project_id)
                return PurgeOutcome.purged(project_id)

        server = create_http_server(
            session_manager=session_manager,
            database=session_manager.db,
        )
        server._runner = SimpleNamespace(project_purge_service=PurgeService())

        response = TestClient(server.app).post(f"/api/projects/{real_project['id']}/purge")

        assert response.status_code == 200
        assert response.json()["status"] == "purged"
        assert calls == [real_project["id"]]

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

    def test_session_manager_unavailable(self, temp_db: HubDatabase) -> None:
        """503 when session_manager is None."""
        server = create_http_server(session_manager=None, database=temp_db)
        client = TestClient(server.app)
        response = client.get("/api/projects")
        assert response.status_code == 503
