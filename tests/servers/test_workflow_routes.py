"""Tests for workflow definitions HTTP API routes."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

SAMPLE_DEFINITION = json.dumps(
    {
        "name": "test-workflow",
        "description": "A test workflow",
        "steps": [{"name": "work", "allowed_tools": "all"}],
    }
)

SAMPLE_PIPELINE_DEFINITION = json.dumps(
    {
        "name": "test-pipeline",
        "type": "pipeline",
        "steps": [{"id": "build", "exec": "make build"}],
    }
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def http_server(temp_db: HubDatabase) -> HTTPServer:
    """Create an HTTP server with a real database."""
    mock_config = MagicMock()
    mock_config.logging.max_size_mb = 10
    mock_config.logging.backup_count = 3
    mock_config.memory.backend = "null"
    mock_config.workflow.timeout = 30
    mock_config.workflow.enabled = True
    mock_config.get_gobby_tasks_config.return_value.enabled = False

    return create_http_server(
        config=mock_config,
        database=temp_db,
        test_mode=True,
    )


@pytest.fixture
def client(http_server: HTTPServer) -> TestClient:
    """Create a test client for the HTTP server."""
    return TestClient(http_server.app)


@pytest.fixture
def manager(temp_db: HubDatabase) -> LocalWorkflowDefinitionManager:
    """Create a workflow definition manager."""
    return LocalWorkflowDefinitionManager(temp_db)


# ============================================================================
# GET /api/workflows — List
# ============================================================================


def test_list_workflows(client: TestClient, manager: LocalWorkflowDefinitionManager) -> None:
    """Test listing workflow definitions."""
    manager.create(name="wf-1", definition_json=SAMPLE_DEFINITION)
    manager.create(name="wf-2", definition_json=SAMPLE_DEFINITION)

    resp = client.get("/api/workflows")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    names = {d["name"] for d in data["definitions"]}
    assert "wf-1" in names
    assert "wf-2" in names


def test_list_workflows_filter_type(
    client: TestClient, manager: LocalWorkflowDefinitionManager
) -> None:
    """Test listing with workflow_type filter."""
    manager.create(name="wf", definition_json=SAMPLE_DEFINITION, workflow_type="workflow")
    manager.create(
        name="pipe", definition_json=SAMPLE_PIPELINE_DEFINITION, workflow_type="pipeline"
    )

    resp = client.get("/api/workflows", params={"workflow_type": "pipeline"})
    assert resp.status_code == 200
    names = {d["name"] for d in resp.json()["definitions"]}
    assert "pipe" in names
    assert "wf" not in names


def test_list_workflows_filter_enabled(
    client: TestClient, manager: LocalWorkflowDefinitionManager
) -> None:
    """Test listing with enabled filter."""
    manager.create(name="on", definition_json=SAMPLE_DEFINITION, enabled=True)
    manager.create(name="off", definition_json=SAMPLE_DEFINITION, enabled=False)

    resp = client.get("/api/workflows", params={"enabled": "true"})
    assert resp.status_code == 200
    names = {d["name"] for d in resp.json()["definitions"]}
    assert "on" in names
    assert "off" not in names


# ============================================================================
# GET /api/workflows/{id} — Get by ID
# ============================================================================


def test_get_workflow_by_id(client: TestClient, manager: LocalWorkflowDefinitionManager) -> None:
    """Test getting a workflow definition by ID."""
    row = manager.create(name="get-test", definition_json=SAMPLE_DEFINITION)

    resp = client.get(f"/api/workflows/{row.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["definition"]["name"] == "get-test"
    assert data["definition"]["id"] == row.id


def test_get_workflow_not_found(client: TestClient) -> None:
    """Test getting a nonexistent workflow returns 404."""
    resp = client.get("/api/workflows/nonexistent-id")
    assert resp.status_code == 404


# ============================================================================
# POST /api/workflows — Create
# ============================================================================


def test_create_workflow(client: TestClient) -> None:
    """Test creating a new workflow definition."""
    resp = client.post(
        "/api/workflows",
        json={
            "name": "new-workflow",
            "definition_json": SAMPLE_DEFINITION,
            "workflow_type": "workflow",
            "description": "A new workflow",
            "priority": 50,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["definition"]["name"] == "new-workflow"
    assert data["definition"]["priority"] == 50
    assert data["definition"]["description"] == "A new workflow"


def test_create_pipeline(client: TestClient) -> None:
    """Test creating a pipeline definition."""
    resp = client.post(
        "/api/workflows",
        json={
            "name": "new-pipeline",
            "definition_json": SAMPLE_PIPELINE_DEFINITION,
            "workflow_type": "pipeline",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["definition"]["workflow_type"] == "pipeline"


# ============================================================================
# PUT /api/workflows/{id} — Update
# ============================================================================


def test_update_workflow(client: TestClient, manager: LocalWorkflowDefinitionManager) -> None:
    """Test updating a workflow definition."""
    row = manager.create(name="update-test", definition_json=SAMPLE_DEFINITION)

    resp = client.put(
        f"/api/workflows/{row.id}",
        json={"description": "Updated", "priority": 25},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["definition"]["description"] == "Updated"
    assert data["definition"]["priority"] == 25


def test_update_workflow_not_found(client: TestClient) -> None:
    """Test updating a nonexistent workflow returns 404."""
    resp = client.put(
        "/api/workflows/nonexistent-id",
        json={"description": "Updated"},
    )
    assert resp.status_code == 404


def test_update_workflow_no_fields(
    client: TestClient, manager: LocalWorkflowDefinitionManager
) -> None:
    """Test updating with no fields returns 400."""
    row = manager.create(name="no-update", definition_json=SAMPLE_DEFINITION)
    resp = client.put(f"/api/workflows/{row.id}", json={})
    assert resp.status_code == 400


# ============================================================================
# DELETE /api/workflows/{id}
# ============================================================================


def test_delete_workflow(client: TestClient, manager: LocalWorkflowDefinitionManager) -> None:
    """Test deleting a workflow definition."""
    row = manager.create(name="delete-test", definition_json=SAMPLE_DEFINITION)

    resp = client.delete(f"/api/workflows/{row.id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Verify it's gone
    resp = client.get(f"/api/workflows/{row.id}")
    assert resp.status_code == 404


def test_delete_workflow_not_found(client: TestClient) -> None:
    """Test deleting a nonexistent workflow returns 404."""
    resp = client.delete("/api/workflows/nonexistent-id")
    assert resp.status_code == 404


# ============================================================================
# POST /api/workflows/import — Import YAML
# ============================================================================


def test_import_yaml(client: TestClient) -> None:
    """Test importing a workflow from YAML content."""
    yaml_content = """\
name: imported-wf
description: From YAML
type: pipeline
steps:
  - id: work
    exec: echo work
"""
    resp = client.post("/api/workflows/import", json={"yaml_content": yaml_content})
    assert resp.status_code == 200
    data = resp.json()
    assert data["definition"]["name"] == "imported-wf"
    assert data["definition"]["source"] == "installed"
    assert data["definition"]["workflow_type"] == "pipeline"


def test_import_yaml_invalid(client: TestClient) -> None:
    """Test importing invalid YAML returns 400."""
    resp = client.post("/api/workflows/import", json={"yaml_content": "not_a_dict: [1, 2]"})
    assert resp.status_code == 400


# ============================================================================
# GET /api/workflows/{id}/export — Export YAML
# ============================================================================


def test_export_yaml(client: TestClient, manager: LocalWorkflowDefinitionManager) -> None:
    """Test exporting a workflow definition as YAML."""
    row = manager.create(
        name="export-test",
        definition_json=json.dumps({"name": "export-test", "steps": []}),
    )

    resp = client.get(f"/api/workflows/{row.id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-yaml"
    assert "export-test" in resp.text


def test_export_yaml_not_found(client: TestClient) -> None:
    """Test exporting a nonexistent workflow returns 404."""
    resp = client.get("/api/workflows/nonexistent-id/export")
    assert resp.status_code == 404


# ============================================================================
# POST /api/workflows/{id}/duplicate
# ============================================================================


def test_duplicate_workflow(client: TestClient, manager: LocalWorkflowDefinitionManager) -> None:
    """Test duplicating a workflow definition."""
    row = manager.create(
        name="original",
        definition_json=SAMPLE_DEFINITION,
        description="Original desc",
        priority=25,
    )

    resp = client.post(
        f"/api/workflows/{row.id}/duplicate",
        json={"new_name": "copy-of-original"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["definition"]["name"] == "copy-of-original"
    assert data["definition"]["description"] == "Original desc"
    assert data["definition"]["id"] != row.id


def test_duplicate_not_found(client: TestClient) -> None:
    """Test duplicating a nonexistent workflow returns 404."""
    resp = client.post(
        "/api/workflows/nonexistent-id/duplicate",
        json={"new_name": "copy"},
    )
    assert resp.status_code == 404


# ============================================================================
# PUT /api/workflows/{id}/toggle — Toggle enabled
# ============================================================================


def test_toggle_enabled(client: TestClient, manager: LocalWorkflowDefinitionManager) -> None:
    """Test toggling a workflow's enabled status."""
    row = manager.create(name="toggle-test", definition_json=SAMPLE_DEFINITION, enabled=True)

    resp = client.put(f"/api/workflows/{row.id}/toggle")
    assert resp.status_code == 200
    assert resp.json()["definition"]["enabled"] is False

    # Toggle back
    resp = client.put(f"/api/workflows/{row.id}/toggle")
    assert resp.status_code == 200
    assert resp.json()["definition"]["enabled"] is True


def test_toggle_not_found(client: TestClient) -> None:
    """Test toggling a nonexistent workflow returns 404."""
    resp = client.put("/api/workflows/nonexistent-id/toggle")
    assert resp.status_code == 404


# ============================================================================
# POST /api/workflows/{id}/move-to-project
# ============================================================================


def test_move_to_project(
    client: TestClient, temp_db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """Test moving a definition to project scope."""
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())",
        ("proj-1", "Test Project"),
    )
    row = manager.create(name="move-proj-test", definition_json=SAMPLE_DEFINITION)

    resp = client.post(
        f"/api/workflows/{row.id}/move-to-project",
        json={"project_id": "proj-1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["definition"]["source"] == "project"
    assert data["definition"]["project_id"] == "proj-1"


def test_move_to_project_not_found(client: TestClient) -> None:
    """Test moving a nonexistent definition returns 404."""
    resp = client.post(
        "/api/workflows/nonexistent-id/move-to-project",
        json={"project_id": "proj-1"},
    )
    assert resp.status_code == 404


# ============================================================================
# POST /api/workflows/{id}/move-to-global
# ============================================================================


def test_move_to_global(
    client: TestClient, temp_db: HubDatabase, manager: LocalWorkflowDefinitionManager
) -> None:
    """Test moving a definition to global scope."""
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, NOW(), NOW())",
        ("proj-1", "Test Project"),
    )
    row = manager.create(
        name="move-global-test",
        definition_json=SAMPLE_DEFINITION,
        source="project",
        project_id="proj-1",
    )

    resp = client.post(f"/api/workflows/{row.id}/move-to-global")
    assert resp.status_code == 200
    data = resp.json()
    assert data["definition"]["source"] == "installed"
    assert data["definition"]["project_id"] is None


def test_move_to_global_not_found(client: TestClient) -> None:
    """Test moving a nonexistent definition returns 404."""
    resp = client.post("/api/workflows/nonexistent-id/move-to-global")
    assert resp.status_code == 404
