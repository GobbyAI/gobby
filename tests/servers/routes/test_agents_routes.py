"""Tests for agent definition API routes - real coverage, minimal mocking.

Exercises src/gobby/servers/routes/agents.py endpoints using
create_http_server() with a real LocalWorkflowDefinitionManager backed by temp_db.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_agent_row(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    description: str | None = None,
    provider: str = "claude",
    mode: str = "autonomous",
    project_id: str | None = None,
    source: str = "template",
    enabled: bool = True,
) -> Any:
    """Create an agent definition row in the DB."""
    body = AgentDefinitionBody(
        name=name,
        description=description or f"Agent {name}",
        provider=provider,
        mode=mode,
        enabled=enabled,
    )
    return manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="agent",
        project_id=project_id,
        description=body.description,
        source=source,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_manager(temp_db) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def agent_manager(temp_db) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(temp_db)


@pytest.fixture
def server(temp_db, task_manager):
    return create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        task_manager=task_manager,
    )


@pytest.fixture
def client(server) -> TestClient:
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# GET /api/agents/definitions  (list)
# ---------------------------------------------------------------------------


class TestListDefinitions:
    def test_list_empty_db(self, client: TestClient) -> None:
        """When no definitions exist, list returns empty."""
        response = client.get("/api/agents/definitions")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["count"] == 0
        assert data["definitions"] == []

    def test_list_with_definitions(
        self, client: TestClient, agent_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _create_agent_row(agent_manager, "list-worker-1")
        _create_agent_row(agent_manager, "list-worker-2")
        response = client.get("/api/agents/definitions")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 2
        names = [d["definition"]["name"] for d in data["definitions"]]
        assert "list-worker-1" in names
        assert "list-worker-2" in names

    def test_list_with_project_filter(
        self, client: TestClient, agent_manager: LocalWorkflowDefinitionManager, project_manager
    ) -> None:
        project = project_manager.create(name="proj-1", repo_path="/tmp/proj-1")
        _create_agent_row(agent_manager, "scoped", project_id=project.id)
        _create_agent_row(agent_manager, "global-agent")
        response = client.get(f"/api/agents/definitions?project_id={project.id}")
        assert response.status_code == 200
        data = response.json()
        # Should include both project-scoped and global agents
        assert data["count"] >= 1

    def test_list_error(self, client: TestClient) -> None:
        """Error during listing returns 500."""
        with patch(
            "gobby.storage.workflow_definitions.LocalWorkflowDefinitionManager.list_all",
            side_effect=RuntimeError("DB error"),
        ):
            response = client.get("/api/agents/definitions")
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/agents/definitions/{name}  (get single)
# ---------------------------------------------------------------------------


class TestGetDefinition:
    def test_get_existing(
        self, client: TestClient, agent_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _create_agent_row(agent_manager, "worker", description="A worker agent")
        response = client.get("/api/agents/definitions/worker")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["definition"]["definition"]["name"] == "worker"

    def test_get_not_found(self, client: TestClient) -> None:
        response = client.get("/api/agents/definitions/nonexistent")
        assert response.status_code == 404

    def test_get_with_project_id(
        self, client: TestClient, agent_manager: LocalWorkflowDefinitionManager, project_manager
    ) -> None:
        project = project_manager.create(name="proj-1", repo_path="/tmp/proj-1")
        _create_agent_row(agent_manager, "scoped", project_id=project.id)
        response = client.get(f"/api/agents/definitions/scoped?project_id={project.id}")
        assert response.status_code == 200

    def test_get_selects_correct_name(
        self, client: TestClient, agent_manager: LocalWorkflowDefinitionManager
    ) -> None:
        """When multiple definitions exist, get returns the one matching name."""
        _create_agent_row(agent_manager, "alpha")
        _create_agent_row(agent_manager, "beta")
        response = client.get("/api/agents/definitions/beta")
        assert response.status_code == 200
        assert response.json()["definition"]["definition"]["name"] == "beta"

    def test_get_error(self, client: TestClient) -> None:
        response = client.get("/api/agents/definitions/nonexistent")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/agents/definitions/{name}/export
# ---------------------------------------------------------------------------


class TestExportDefinition:
    def test_export_existing(
        self, client: TestClient, agent_manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Export serializes agent definition as YAML."""
        _create_agent_row(agent_manager, "worker", provider="claude")
        response = client.get("/api/agents/definitions/worker/export")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/x-yaml"
        assert "attachment" in response.headers.get("content-disposition", "")
        assert "name: worker" in response.text
        assert "provider: claude" in response.text

    def test_export_db_backed(
        self, client: TestClient, agent_manager: LocalWorkflowDefinitionManager
    ) -> None:
        """DB-backed definitions serialize correctly."""
        _create_agent_row(agent_manager, "db-agent", source="installed")
        response = client.get("/api/agents/definitions/db-agent/export")
        assert response.status_code == 200
        assert "name: db-agent" in response.text

    def test_export_not_found(self, client: TestClient) -> None:
        response = client.get("/api/agents/definitions/missing/export")
        assert response.status_code == 404

    def test_export_error(self, client: TestClient) -> None:
        # Create an agent then verify export with nonexistent name still 404s
        response = client.get("/api/agents/definitions/nonexistent/export")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/agents/definitions  (create in DB)
# ---------------------------------------------------------------------------


class TestCreateDefinition:
    def test_create_basic(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json={"name": "new-agent"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["definition"]["name"] == "new-agent"

    def test_create_with_all_fields(self, client: TestClient) -> None:
        response = client.post(
            "/api/agents/definitions",
            json={
                "name": "full-agent",
                "description": "Full test",
                "role": "tester",
                "goal": "test things",
                "provider": "gemini",
                "model": "flash",
                "mode": "terminal",
                "isolation": "worktree",
                "base_branch": "develop",
                "timeout": 300.0,
                "max_turns": 20,
            },
        )
        assert response.status_code == 200
        defn = response.json()["definition"]
        assert defn["name"] == "full-agent"
        assert defn["description"] == "Full test"

    def test_create_with_project_id(self, client: TestClient, project_manager) -> None:
        project = project_manager.create(name="test-proj", repo_path="/tmp/test-proj")
        response = client.post(
            "/api/agents/definitions",
            json={"name": "proj-agent", "project_id": project.id},
        )
        assert response.status_code == 200
        assert response.json()["definition"]["project_id"] == project.id

    def test_create_duplicate_name_fails(self, client: TestClient) -> None:
        client.post("/api/agents/definitions", json={"name": "dup"})
        response = client.post("/api/agents/definitions", json={"name": "dup"})
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# PUT /api/agents/definitions/{id}
# ---------------------------------------------------------------------------


class TestUpdateDefinition:
    def test_update_fields(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json={"name": "updatable"}
        ).json()["definition"]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"description": "Updated"},
        )
        assert response.status_code == 200
        assert response.json()["definition"]["description"] == "Updated"

    def test_update_no_fields_returns_400(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json={"name": "no-update"}
        ).json()["definition"]
        response = client.put(f"/api/agents/definitions/{created['id']}", json={})
        assert response.status_code == 400

    def test_update_not_found(self, client: TestClient) -> None:
        response = client.put(
            "/api/agents/definitions/nonexistent-id",
            json={"description": "X"},
        )
        assert response.status_code == 404

    def test_update_enabled_field(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json={"name": "toggle-me"}
        ).json()["definition"]
        response = client.put(
            f"/api/agents/definitions/{created['id']}", json={"enabled": False}
        )
        assert response.status_code == 200
        assert response.json()["definition"]["enabled"] is False

    def test_update_body_fields(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json={"name": "body-update"}
        ).json()["definition"]
        response = client.put(
            f"/api/agents/definitions/{created['id']}",
            json={"model": "opus", "timeout": 600.0},
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# DELETE /api/agents/definitions/{id}
# ---------------------------------------------------------------------------


class TestDeleteDefinition:
    def test_delete_existing(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json={"name": "deletable"}
        ).json()["definition"]
        response = client.delete(f"/api/agents/definitions/{created['id']}")
        assert response.status_code == 200
        assert response.json()["deleted"] is True

    def test_delete_not_found(self, client: TestClient) -> None:
        response = client.delete("/api/agents/definitions/nonexistent-id")
        assert response.status_code == 404

    def test_delete_idempotent(self, client: TestClient) -> None:
        created = client.post(
            "/api/agents/definitions", json={"name": "del-twice"}
        ).json()["definition"]
        client.delete(f"/api/agents/definitions/{created['id']}")
        response = client.delete(f"/api/agents/definitions/{created['id']}")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/agents/definitions/import/{name}
# ---------------------------------------------------------------------------


class TestImportDefinition:
    def test_import_from_file(self, client: TestClient, tmp_path: Path) -> None:
        """Import a file-based definition into the DB."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "importable.yaml").write_text(
            "name: importable\ndescription: Imported agent\nprovider: claude\nmode: autonomous\n"
        )

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/importable")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["definition"]["name"] == "importable"

    def test_import_not_found(self, client: TestClient, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/missing")
        assert response.status_code == 404

    def test_import_with_project_id(self, client: TestClient, project_manager, tmp_path: Path) -> None:
        project = project_manager.create(name="import-proj", repo_path="/tmp/import-proj")
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "proj-agent.yaml").write_text(
            "name: proj-agent\nprovider: claude\nmode: autonomous\n"
        )

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post(
                f"/api/agents/definitions/import/proj-agent?project_id={project.id}"
            )
        assert response.status_code == 200
        assert response.json()["definition"]["project_id"] == project.id

    def test_import_error(self, client: TestClient, tmp_path: Path) -> None:
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # Write invalid YAML that will parse but fail AgentDefinitionBody validation
        (agents_dir / "broken.yaml").write_text("- not a dict\n")

        with patch(
            "gobby.agents.sync.get_bundled_agents_path",
            return_value=agents_dir,
        ):
            response = client.post("/api/agents/definitions/import/broken")
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


class TestCrudRoundTrip:
    def test_create_list_update_delete(self, client: TestClient) -> None:
        # Create
        resp = client.post(
            "/api/agents/definitions",
            json={"name": "lifecycle-test", "description": "Round-trip"},
        )
        assert resp.status_code == 200
        defn = resp.json()["definition"]
        defn_id = defn["id"]

        # List
        resp = client.get("/api/agents/definitions")
        assert resp.status_code == 200
        names = [d["definition"]["name"] for d in resp.json()["definitions"]]
        assert "lifecycle-test" in names

        # Update
        resp = client.put(
            f"/api/agents/definitions/{defn_id}",
            json={"description": "Updated round-trip"},
        )
        assert resp.status_code == 200

        # Delete
        resp = client.delete(f"/api/agents/definitions/{defn_id}")
        assert resp.status_code == 200
