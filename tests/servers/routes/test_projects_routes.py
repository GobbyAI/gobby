"""Tests for project API routes with real database objects."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gobby.projects.purge import PurgeOutcome
from gobby.servers.routes import projects as projects_routes
from gobby.storage.external_issue_sync import ExternalIssueSyncStatusStore
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from tests.fixtures.isolated_checkout import (
    IsolatedCheckoutProject,
    insert_isolated_machine,
    insert_overlay,
    install_isolated_checkout_project,
    write_project_marker,
)
from tests.servers.conftest import create_http_server

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"
FOREIGN_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _http_error(response: Any) -> str:
    payload = response.json()
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        return str(detail.get("error") or "")
    return str(detail)


def _install_local_checkout(
    db: HubDatabase,
    root: Path,
    *,
    name: str,
) -> IsolatedCheckoutProject:
    return install_isolated_checkout_project(
        db,
        root,
        name=name,
        machine_id=LOCAL_MACHINE_ID,
    )


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


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
            machine_id="21000000-0000-4000-8000-000000000002",
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
                machine_id="21000000-0000-4000-8000-000000000002",
                project_id=project.id,
            )
            task_manager.create_task(
                project_id=project.id,
                title=f"Open task {index}",
                validation_criteria="Test task completion is observable.",
            )

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

    def test_update_project_ignores_repo_path(self, client: TestClient, real_project: dict) -> None:
        """repo_path is not a project JSON field and cannot be updated here."""
        with TestClient(client.app, raise_server_exceptions=False) as http:
            response = http.put(
                f"/api/projects/{real_project['id']}",
                json={"repo_path": "/new/path"},
            )
        assert response.status_code == 200
        data = response.json()
        assert "repo_path" not in data
        assert data["checkout"] is None

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

    @pytest.mark.parametrize("binding_field", ["linear_team_id", "linear_project_id"])
    def test_update_project_rejects_clearing_effective_linear_binding(
        self,
        client: TestClient,
        real_project: dict[str, Any],
        binding_field: str,
    ) -> None:
        enable_response = client.patch(
            f"/api/projects/{real_project['id']}",
            json={
                "linear_team_id": "team-1",
                "linear_project_id": "linear-project-1",
                "linear_sync_enabled": True,
            },
        )
        assert enable_response.status_code == 200

        response = client.patch(
            f"/api/projects/{real_project['id']}",
            json={binding_field: None},
        )

        assert response.status_code == 400
        assert "linear_team_id and linear_project_id" in response.json()["detail"]

    def test_integrations_status_reports_live_counts_before_first_run(
        self,
        client: TestClient,
        real_project: dict,
        session_manager: SessionManager,
    ) -> None:
        task_manager = LocalTaskManager(session_manager.db)
        task_manager.create_task(
            project_id=real_project["id"],
            title="Pending",
            validation_criteria="Test task completion is observable.",
        )
        task_manager.create_task(
            project_id=real_project["id"],
            title="Linked",
            linear_issue_id="linear-1",
            github_repo="test/my-project",
            github_issue_number=1,
            validation_criteria="Test task completion is observable.",
        )

        response = client.get(f"/api/projects/{real_project['id']}/integrations/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["linear"]["state"] == "pending"
        assert payload["linear"]["linked_count"] == 1
        assert payload["linear"]["pending_count"] == 1
        assert payload["linear"]["last_outbound_success_at"] is None
        assert payload["github"]["linked_count"] == 1
        assert payload["github"]["pending_count"] == 0
        assert payload["github"]["last_outbound_success_at"] is None
        assert payload["github"]["readiness_error"] == "GitHub connector is unavailable"

    def test_integrations_status_normalizes_provider_payloads_and_repository_fallback(
        self,
        client: TestClient,
        real_project: dict[str, Any],
        session_manager: SessionManager,
    ) -> None:
        update_response = client.patch(
            f"/api/projects/{real_project['id']}",
            json={"github_repo": "test/my-project"},
        )
        assert update_response.status_code == 200

        status_store = ExternalIssueSyncStatusStore(session_manager.db)
        for provider in ("linear", "github"):
            status_store.upsert(
                project_id=real_project["id"],
                provider=provider,
                state="healthy",
                linked_count=0,
                pending_count=0,
            )

        response = client.get(f"/api/projects/{real_project['id']}/integrations/status")

        assert response.status_code == 200
        payload = response.json()
        assert payload["github"]["repositories"] == ["test/my-project"]
        for provider in ("linear", "github"):
            assert "project_id" not in payload[provider]
            assert "provider" not in payload[provider]
            assert "last_outbound_success_at" in payload[provider]

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
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "rules-repo",
            name=_unique_name("rules-project"),
        )
        project = isolated.project

        response = client.put(
            f"/api/projects/{project.id}",
            json={"approval_rules": ["tool:Write", " tool:Write ", "mcp:third-party:*"]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["approval_rules"] == ["tool:Write", "mcp:third-party:*"]
        assert data["checkout"] == {
            "machine_id": LOCAL_MACHINE_ID,
            "root_path": isolated.root_path,
        }

        project_file = Path(isolated.root_path) / ".gobby" / "project.json"
        assert project_file.exists()
        saved = json.loads(project_file.read_text())
        assert saved["tool_approvals"]["allow"] == ["tool:Write", "mcp:third-party:*"]

    def test_update_project_approval_rules_requires_local_checkout(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
    ) -> None:
        project = project_manager.create(name=_unique_name("no-checkout-project"))

        response = client.put(
            f"/api/projects/{project.id}",
            json={"approval_rules": ["tool:Write"]},
        )
        assert response.status_code == 409
        assert _http_error(response) == "CheckoutNotFoundError"

    def test_update_project_validation_detection(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "validation-repo",
            name=_unique_name("validation-detection-project"),
        )
        project = isolated.project

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
        project_file = Path(isolated.root_path) / ".gobby" / "project.json"
        saved = json.loads(project_file.read_text())
        assert saved["validation_detection"]["custom_matchers"][0]["id"] == "project-ci"

    def test_update_project_invalid_validation_detection_does_not_mutate(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = project_manager.create(name="unchanged")

        def fail_mutation(*args: object, **kwargs: object) -> None:
            raise AssertionError("invalid request attempted a project-file mutation")

        for function_name in (
            "save_project_approval_rules",
            "save_project_validation_detection",
        ):
            monkeypatch.setattr(projects_routes, function_name, fail_mutation)

        response = client.put(
            f"/api/projects/{project.id}",
            json={
                "name": "changed",
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

    def test_update_project_file_failure_rolls_back_database_update(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "rollback-repo",
            name=_unique_name("unchanged"),
        )
        project = isolated.project

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
        assert persisted.name == project.name

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
            json={"github_url": "https://github.com/test/personal"},
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
        runner = SimpleNamespace(project_purge_service=PurgeService())
        server.set_runner_getter(lambda: cast(Any, runner))

        response = TestClient(server.app).post(f"/api/projects/{real_project['id']}/purge")

        assert response.status_code == 200
        assert response.json()["status"] == "purged"
        assert calls == [real_project["id"]]

    def test_purge_project_returns_503_without_runner_service(
        self,
        session_manager: SessionManager,
        real_project: dict,
    ) -> None:
        server = create_http_server(
            session_manager=session_manager,
            database=session_manager.db,
        )
        runner = SimpleNamespace(project_purge_service=None)
        server.set_runner_getter(lambda: cast(Any, runner))

        response = TestClient(server.app).post(f"/api/projects/{real_project['id']}/purge")

        assert response.status_code == 503
        assert response.json() == {"detail": "Project purge service is unavailable"}

    @pytest.mark.parametrize(
        ("outcome_factory", "status_code", "status", "message"),
        [
            (PurgeOutcome.protected, 403, "protected", "project is protected"),
            (PurgeOutcome.failed, 500, "failed", "purge failed"),
        ],
    )
    def test_purge_project_maps_unsuccessful_outcomes(
        self,
        session_manager: SessionManager,
        real_project: dict[str, Any],
        outcome_factory: Callable[[str, str], PurgeOutcome],
        status_code: int,
        status: str,
        message: str,
    ) -> None:
        project_id = real_project["id"]

        class PurgeService:
            async def purge_project(self, requested_id: str) -> PurgeOutcome:
                assert requested_id == project_id
                return outcome_factory(requested_id, message)

        server = create_http_server(
            session_manager=session_manager,
            database=session_manager.db,
        )
        runner = SimpleNamespace(project_purge_service=PurgeService())
        server.set_runner_getter(lambda: cast(Any, runner))

        response = TestClient(server.app).post(f"/api/projects/{project_id}/purge")

        assert response.status_code == status_code
        assert response.json() == {
            "detail": {
                "project_id": project_id,
                "status": status,
                "message": message,
            }
        }

    def test_delete_protected_personal(self, client: TestClient, personal_project: dict) -> None:
        """Cannot delete _personal (system project)."""
        response = client.delete(f"/api/projects/{personal_project['id']}")
        assert response.status_code == 403

    def test_delete_protected_orphaned(
        self, client: TestClient, orphaned_project: dict[str, Any]
    ) -> None:
        """Cannot delete _orphaned (system project)."""
        response = client.delete(f"/api/projects/{orphaned_project['id']}")
        assert response.status_code == 403

    def test_delete_protected_migrated(
        self, client: TestClient, migrated_project: dict[str, Any]
    ) -> None:
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


class TestProjectCheckoutHttp:
    """§ 2.4 checkout HTTP routes and path-free project JSON.

    Named methods are the TDD acceptance symbols for close.
    """

    @pytest.fixture
    def client(
        self, session_manager: SessionManager, project_manager: LocalProjectManager
    ) -> TestClient:
        server = create_http_server(
            session_manager=session_manager,
            database=session_manager.db,
        )
        return TestClient(server.app)

    def test_project_json_has_calling_checkout_not_repo_path(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        bare = project_manager.create(name=_unique_name("bare-json"))
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "json-repo",
            name=_unique_name("json-checkout"),
        )
        require_root = patch("gobby.storage.project_checkouts.require_root")
        routes_require_root = patch.object(projects_routes, "require_root", create=True)
        with require_root as mocked_root, routes_require_root as mocked_routes_root:
            mocked_root.side_effect = AssertionError("require_root must not serialize")
            mocked_routes_root.side_effect = AssertionError("require_root must not serialize")
            bare_response = client.get(f"/api/projects/{bare.id}")
            isolated_response = client.get(f"/api/projects/{isolated.project.id}")
            sentinel_response = client.get(f"/api/projects/{PERSONAL_PROJECT_ID}")
            listed = client.get("/api/projects")

        assert bare_response.status_code == 200
        bare_data = bare_response.json()
        assert "repo_path" not in bare_data
        assert "checkout" in bare_data
        assert bare_data["checkout"] is None
        assert bare_data["approval_rules"] == []
        assert bare_data["validation_detection"] is None

        assert isolated_response.status_code == 200
        isolated_data = isolated_response.json()
        assert "repo_path" not in isolated_data
        assert isolated_data["checkout"] == {
            "machine_id": LOCAL_MACHINE_ID,
            "root_path": isolated.root_path,
        }

        assert sentinel_response.status_code == 200
        sentinel_data = sentinel_response.json()
        assert "repo_path" not in sentinel_data
        assert "checkout" in sentinel_data
        assert sentinel_data["checkout"] is None
        assert sentinel_data["approval_rules"] == []

        assert listed.status_code == 200
        listed_ids = {item["id"]: item for item in listed.json()}
        assert "checkout" in listed_ids[bare.id]
        assert listed_ids[bare.id]["checkout"] is None
        assert listed_ids[isolated.project.id]["checkout"] == {
            "machine_id": LOCAL_MACHINE_ID,
            "root_path": isolated.root_path,
        }

    def test_get_checkouts_returns_calling_daemon_object_or_null(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "get-checkout",
            name=_unique_name("get-checkout"),
        )
        foreign = insert_isolated_machine(project_manager.db, FOREIGN_MACHINE_ID)
        LocalProjectCheckoutManager(project_manager.db).register(
            foreign, isolated.project.id, "/foreign/root"
        )
        missing = client.get("/api/projects/00000000-0000-4000-8000-000000000099/checkouts")
        present = client.get(f"/api/projects/{isolated.project.id}/checkouts")
        sentinel = client.get(f"/api/projects/{PERSONAL_PROJECT_ID}/checkouts")
        bare = project_manager.create(name=_unique_name("get-null"))
        null_checkout = client.get(f"/api/projects/{bare.id}/checkouts")

        assert missing.status_code == 404
        assert present.status_code == 200
        assert present.json() == {
            "checkout": {
                "machine_id": LOCAL_MACHINE_ID,
                "root_path": isolated.root_path,
            }
        }
        assert "/foreign/root" not in json.dumps(present.json())
        assert sentinel.status_code == 200
        assert sentinel.json() == {"checkout": None}
        assert null_checkout.status_code == 200
        assert null_checkout.json() == {"checkout": None}

    def test_register_is_201_then_200_and_maps_typed_errors(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "register-root",
            name=_unique_name("register-http"),
        )
        project_manager.db.execute(
            "DELETE FROM project_checkouts WHERE project_id = %s AND machine_id = %s",
            (isolated.project.id, LOCAL_MACHINE_ID),
        )
        url = f"/api/projects/{isolated.project.id}/checkouts"
        body = {"root_path": isolated.root_path}
        created = client.post(url, json=body)
        retry = client.post(url, json=body)
        assert created.status_code == 201
        assert created.json() == {
            "checkout": {
                "machine_id": LOCAL_MACHINE_ID,
                "root_path": isolated.root_path,
            }
        }
        assert retry.status_code == 200
        assert retry.json() == created.json()

        other = tmp_path / "other-root"
        other.mkdir()
        write_project_marker(other, project_id=isolated.project.id, name=isolated.project.name)
        conflict = client.post(url, json={"root_path": str(other)})
        assert conflict.status_code == 409
        assert _http_error(conflict) == "CheckoutConflictError"

        overlay_root = tmp_path / "register-overlay"
        overlay_root.mkdir()
        write_project_marker(
            overlay_root, project_id=isolated.project.id, name=isolated.project.name
        )
        insert_overlay(
            project_manager.db,
            project_id=isolated.project.id,
            machine_id=LOCAL_MACHINE_ID,
            path=str(overlay_root),
            kind="worktree",
        )
        overlay = client.post(url, json={"root_path": str(overlay_root)})
        assert overlay.status_code == 409
        assert _http_error(overlay) == "OverlayRegistrationRejectedError"

        mismatch = tmp_path / "register-mismatch"
        mismatch.mkdir()
        write_project_marker(mismatch, project_id=str(uuid.uuid4()), name="other")
        marker = client.post(url, json={"root_path": str(mismatch)})
        assert marker.status_code == 409
        assert _http_error(marker) == "MarkerMismatchError"

        relative = client.post(url, json={"root_path": "relative/path"})
        home = client.post(url, json={"root_path": "~/repo"})
        missing_dir = client.post(url, json={"root_path": str(tmp_path / "no-such-root")})
        for response in (relative, home, missing_dir):
            assert response.status_code == 400
            assert _http_error(response) == "InvalidCheckoutRootError"

        sentinel = client.post(
            f"/api/projects/{PERSONAL_PROJECT_ID}/checkouts",
            json={"root_path": isolated.root_path},
        )
        assert sentinel.status_code == 409
        assert _http_error(sentinel) == "CheckoutSentinelRejectedError"

        def _no_machine() -> str:
            raise RuntimeError("Local machine ID is unavailable")

        monkeypatch.setattr(
            "gobby.storage.workspace_machine_scope.require_machine_id",
            _no_machine,
        )
        unavailable = client.post(url, json=body)
        assert unavailable.status_code == 409
        assert _http_error(unavailable) == "MissingMachineContextError"
        assert "RuntimeError" not in unavailable.text

    def test_concurrent_same_root_register_yields_one_201(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "concurrent-root",
            name=_unique_name("concurrent-register"),
        )
        project_manager.db.execute(
            "DELETE FROM project_checkouts WHERE project_id = %s AND machine_id = %s",
            (isolated.project.id, LOCAL_MACHINE_ID),
        )
        url = f"/api/projects/{isolated.project.id}/checkouts"
        body = {"root_path": isolated.root_path}
        app = client.app

        def _post() -> int:
            with TestClient(app, raise_server_exceptions=False) as nested:
                return nested.post(url, json=body).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: _post(), range(4)))
        assert statuses.count(201) == 1, statuses
        assert set(statuses) <= {200, 201}, statuses
        assert statuses.count(200) == len(statuses) - 1, statuses

    def test_rebind_is_200_and_rejects_foreign_overlay_and_mismatch(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "rebind-first",
            name=_unique_name("rebind-http"),
        )
        second = tmp_path / "rebind-second"
        second.mkdir()
        write_project_marker(second, project_id=isolated.project.id, name=isolated.project.name)
        overlay_root = tmp_path / "overlay"
        overlay_root.mkdir()
        write_project_marker(
            overlay_root, project_id=isolated.project.id, name=isolated.project.name
        )
        insert_overlay(
            project_manager.db,
            project_id=isolated.project.id,
            machine_id=LOCAL_MACHINE_ID,
            path=str(overlay_root),
            kind="worktree",
        )
        mismatch = tmp_path / "mismatch"
        mismatch.mkdir()
        write_project_marker(mismatch, project_id=str(uuid.uuid4()), name="other")
        taken = project_manager.create(name=_unique_name("root-taken"))
        taken_root = tmp_path / "taken-root"
        taken_root.mkdir()
        write_project_marker(taken_root, project_id=taken.id, name=taken.name)

        rebind_url = f"/api/projects/{isolated.project.id}/checkouts/{LOCAL_MACHINE_ID}/rebind"
        rebound = client.post(rebind_url, json={"root_path": str(second)})
        assert rebound.status_code == 200
        assert rebound.json() == {
            "checkout": {"machine_id": LOCAL_MACHINE_ID, "root_path": str(second)}
        }
        persisted_checkout = LocalProjectCheckoutManager(project_manager.db).get(
            LOCAL_MACHINE_ID, isolated.project.id
        )
        assert persisted_checkout is not None
        assert persisted_checkout.root_path == str(second)

        overlay = client.post(rebind_url, json={"root_path": str(overlay_root)})
        assert overlay.status_code == 409
        assert _http_error(overlay) == "OverlayRegistrationRejectedError"

        marker = client.post(rebind_url, json={"root_path": str(mismatch)})
        assert marker.status_code == 409
        assert _http_error(marker) == "MarkerMismatchError"

        first = client.post(
            f"/api/projects/{taken.id}/checkouts",
            json={"root_path": str(taken_root)},
        )
        assert first.status_code == 201
        write_project_marker(taken_root, project_id=isolated.project.id, name=isolated.project.name)
        taken_conflict = client.post(rebind_url, json={"root_path": str(taken_root)})
        assert taken_conflict.status_code == 409
        assert _http_error(taken_conflict) == "CheckoutRootTakenError"

        filesystem_calls: list[str] = []

        def _boom(*args: object, **kwargs: object) -> str:
            filesystem_calls.append("validate")
            raise AssertionError("foreign machine must not touch the filesystem")

        monkeypatch.setattr("gobby.utils.checkout_root.validate_checkout_root", _boom)
        monkeypatch.setattr(projects_routes, "validate_checkout_root", _boom, raising=False)
        foreign = client.post(
            f"/api/projects/{isolated.project.id}/checkouts/{FOREIGN_MACHINE_ID}/rebind",
            json={"root_path": str(second)},
        )
        assert foreign.status_code == 409
        assert filesystem_calls == []
        assert _http_error(foreign) in {
            "MachineOwnershipMismatchError",
            "MissingMachineContextError",
        }

        def _no_machine() -> str:
            raise RuntimeError("Local machine ID is unavailable")

        monkeypatch.setattr(
            "gobby.storage.workspace_machine_scope.require_machine_id",
            _no_machine,
        )
        unavailable = client.post(rebind_url, json={"root_path": str(second)})
        assert unavailable.status_code == 409
        assert _http_error(unavailable) == "MissingMachineContextError"
        assert "RuntimeError" not in unavailable.text

    def test_settings_use_local_checkout_never_other_machine(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "settings-local",
            name=_unique_name("settings-local"),
        )
        loaded = client.get(f"/api/projects/{isolated.project.id}")
        assert loaded.status_code == 200
        assert loaded.json()["approval_rules"] == []

        written = client.put(
            f"/api/projects/{isolated.project.id}",
            json={"approval_rules": ["tool:Write"]},
        )
        assert written.status_code == 200
        assert written.json()["approval_rules"] == ["tool:Write"]
        saved = json.loads((Path(isolated.root_path) / ".gobby" / "project.json").read_text())
        assert saved["tool_approvals"]["allow"] == ["tool:Write"]

        foreign_only = project_manager.create(name=_unique_name("foreign-settings"))
        foreign_root = tmp_path / "foreign-settings"
        foreign_root.mkdir()
        write_project_marker(foreign_root, project_id=foreign_only.id, name=foreign_only.name)
        (foreign_root / ".gobby" / "project.json").write_text(
            json.dumps(
                {
                    "id": foreign_only.id,
                    "name": foreign_only.name,
                    "tool_approvals": {"allow": ["tool:Bash"]},
                }
            )
        )
        LocalProjectCheckoutManager(project_manager.db).register(
            insert_isolated_machine(project_manager.db, FOREIGN_MACHINE_ID),
            foreign_only.id,
            str(foreign_root),
        )

        def fail_load(*args: object, **kwargs: object) -> list[str]:
            raise AssertionError("null-checkout reads must not touch the filesystem")

        monkeypatch.setattr(projects_routes, "load_project_approval_rules", fail_load)
        monkeypatch.setattr(projects_routes, "load_project_validation_detection", fail_load)
        monkeypatch.setattr(
            "gobby.storage.project_checkouts.require_root",
            fail_load,
        )
        null_read = client.get(f"/api/projects/{foreign_only.id}")
        assert null_read.status_code == 200
        assert null_read.json()["checkout"] is None
        assert null_read.json()["approval_rules"] == []
        assert null_read.json()["validation_detection"] is None

        monkeypatch.undo()
        missing_write = client.put(
            f"/api/projects/{foreign_only.id}",
            json={"approval_rules": ["tool:Write"]},
        )
        assert missing_write.status_code == 409
        assert _http_error(missing_write) == "CheckoutNotFoundError"
        foreign_saved = json.loads((foreign_root / ".gobby" / "project.json").read_text())
        assert foreign_saved["tool_approvals"]["allow"] == ["tool:Bash"]

    def test_register_refuses_soft_deleted_rebind_preserves_deleted_at(
        self,
        client: TestClient,
        project_manager: LocalProjectManager,
        tmp_path: Path,
    ) -> None:
        isolated = _install_local_checkout(
            project_manager.db,
            tmp_path / "deleted-root",
            name=_unique_name("deleted-http"),
        )
        second = tmp_path / "deleted-second"
        second.mkdir()
        write_project_marker(second, project_id=isolated.project.id, name=isolated.project.name)
        assert project_manager.soft_delete(isolated.project.id) is True
        deleted_at = project_manager.get(isolated.project.id)
        assert deleted_at is not None
        assert deleted_at.deleted_at is not None
        original_deleted_at = deleted_at.deleted_at

        register = client.post(
            f"/api/projects/{isolated.project.id}/checkouts",
            json={"root_path": isolated.root_path},
        )
        assert register.status_code == 409
        assert _http_error(register) == "SoftDeletedProjectRejectedError"
        after_register = project_manager.get(isolated.project.id)
        assert after_register is not None
        assert after_register.deleted_at == original_deleted_at

        rebind = client.post(
            f"/api/projects/{isolated.project.id}/checkouts/{LOCAL_MACHINE_ID}/rebind",
            json={"root_path": str(second)},
        )
        assert rebind.status_code == 200
        assert rebind.json()["checkout"]["root_path"] == str(second)
        after_rebind = project_manager.get(isolated.project.id)
        assert after_rebind is not None
        assert after_rebind.deleted_at == original_deleted_at
