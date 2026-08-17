"""HTTP tests for /api/pipelines/definitions (plan 5.1)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.definitions._shared import compute_definition_hash
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.template_hashes import TemplateHashCache
from tests.servers.conftest import create_http_server

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("authenticated_http_requests")]

UNKNOWN_ID = "99999999-9999-4999-8999-999999999999"
REPO_ROOT = Path(__file__).resolve().parents[3]

SAMPLE_PIPELINE: dict[str, Any] = {
    "name": "test-pipeline",
    "type": "pipeline",
    "description": "A test pipeline",
    "version": "1.0",
    "steps": [{"id": "build", "exec": "make build"}],
}


@pytest.fixture
def pipe_manager(temp_db: HubDatabase) -> PipelineDefinitionManager:
    return PipelineDefinitionManager(temp_db)


@pytest.fixture
def server(temp_db: HubDatabase) -> Any:
    return create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
    )


@pytest.fixture
def client(server: Any) -> TestClient:
    return TestClient(server.app)


def _create_pipeline(
    manager: PipelineDefinitionManager,
    **kwargs: Any,
) -> Any:
    defaults: dict[str, Any] = {
        "name": "test-pipeline",
        "definition_json": dict(SAMPLE_PIPELINE),
        "description": "A test pipeline",
        "version": "1.0",
        "source": "custom",
    }
    defaults.update(kwargs)
    return manager.create(**defaults)


def test_pipeline_definitions_router_is_importable() -> None:
    module = importlib.import_module("gobby.servers.routes.pipeline_definitions")
    assert callable(module.create_pipeline_definitions_router)


def test_generic_workflows_router_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("gobby.servers.routes.workflows")
    import gobby.servers.routes as routes

    assert not hasattr(routes, "create_workflows_router")


def test_workflow_templates_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("gobby.workflows.workflow_templates")


def test_generic_workflow_route_suites_are_gone() -> None:
    assert not (REPO_ROOT / "tests/servers/routes/test_workflows.py").exists()
    assert not (REPO_ROOT / "tests/servers/test_workflow_routes.py").exists()
    assert not (REPO_ROOT / "tests/workflows/test_workflow_templates.py").exists()


def test_api_workflows_is_not_registered(client: TestClient) -> None:
    resp = client.get("/api/workflows")
    assert resp.status_code == 404


def test_definitions_mount_before_execution_router(client: TestClient) -> None:
    """GET /api/pipelines/definitions must not be shadowed by GET /{execution_id}."""
    resp = client.get("/api/pipelines/definitions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "definitions" in body
    assert "execution_id" not in body


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/api/pipelines/definitions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["definitions"] == []


def test_list_filters_enabled_and_include_deleted(
    client: TestClient,
    pipe_manager: PipelineDefinitionManager,
) -> None:
    live = _create_pipeline(pipe_manager, name="live-pipe")
    disabled = _create_pipeline(pipe_manager, name="off-pipe", enabled=False)
    deleted = _create_pipeline(pipe_manager, name="gone-pipe")
    pipe_manager.delete(deleted.id)

    enabled_only = client.get("/api/pipelines/definitions", params={"enabled": "true"})
    assert enabled_only.status_code == 200
    names = {row["name"] for row in enabled_only.json()["definitions"]}
    assert live.name in names
    assert disabled.name not in names
    assert deleted.name not in names

    with_deleted = client.get("/api/pipelines/definitions", params={"include_deleted": "true"})
    assert with_deleted.status_code == 200
    names = {row["name"] for row in with_deleted.json()["definitions"]}
    assert {live.name, disabled.name, deleted.name} <= names


def test_list_annotates_template_drift_by_kind(
    client: TestClient,
    pipe_manager: PipelineDefinitionManager,
) -> None:
    row = _create_pipeline(pipe_manager, name="drifted-pipe")
    cache = TemplateHashCache()
    cache._hashes[("pipeline", row.name)] = compute_definition_hash('{"name":"other"}')
    cache._json_cache[("pipeline", row.name)] = '{"name":"other"}'
    from unittest.mock import patch

    with patch(
        "gobby.workflows.template_hashes.get_template_hash_cache",
        return_value=cache,
    ):
        resp = client.get("/api/pipelines/definitions")
    assert resp.status_code == 200
    listed = resp.json()["definitions"][0]
    assert listed["kind"] == "pipeline"
    assert listed["has_template_update"] is True


def test_get_templates(client: TestClient) -> None:
    resp = client.get("/api/pipelines/definitions/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    ids = {item["id"] for item in data["templates"]}
    assert {"blank-pipeline", "ci-pipeline"} <= ids
    for item in data["templates"]:
        assert item["kind"] == "pipeline"
        assert "workflow_type" not in item


def test_create_get_update_toggle_delete_cycle(
    client: TestClient,
    pipe_manager: PipelineDefinitionManager,
) -> None:
    created = client.post(
        "/api/pipelines/definitions",
        json={
            "name": "cycle-pipe",
            "definition_json": json.dumps(SAMPLE_PIPELINE),
            "description": "cycle",
        },
    )
    assert created.status_code == 200
    definition_id = created.json()["definition"]["id"]

    fetched = client.get(f"/api/pipelines/definitions/{definition_id}")
    assert fetched.status_code == 200
    assert fetched.json()["definition"]["name"] == "cycle-pipe"

    updated = client.put(
        f"/api/pipelines/definitions/{definition_id}",
        json={"description": "updated"},
    )
    assert updated.status_code == 200
    assert updated.json()["definition"]["description"] == "updated"


def test_update_strips_reserved_gobby_tag(client: TestClient) -> None:
    created = client.post(
        "/api/pipelines/definitions",
        json={
            "name": "tagged-pipe",
            "definition_json": json.dumps(SAMPLE_PIPELINE),
            "tags": ["custom"],
        },
    )
    assert created.status_code == 200
    definition_id = created.json()["definition"]["id"]

    updated = client.put(
        f"/api/pipelines/definitions/{definition_id}",
        json={"tags": ["keep", "gobby", "ops"]},
    )
    assert updated.status_code == 200
    assert updated.json()["definition"]["tags"] == ["keep", "ops"]

    toggled = client.put(f"/api/pipelines/definitions/{definition_id}/toggle")
    assert toggled.status_code == 200
    assert toggled.json()["definition"]["enabled"] is False

    deleted = client.delete(f"/api/pipelines/definitions/{definition_id}")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/pipelines/definitions/{definition_id}").status_code == 404


def test_duplicate_import_export_restore(
    client: TestClient,
    pipe_manager: PipelineDefinitionManager,
) -> None:
    row = _create_pipeline(pipe_manager, name="source-pipe")
    duplicated = client.post(
        f"/api/pipelines/definitions/{row.id}/duplicate",
        json={"new_name": "copy-pipe"},
    )
    assert duplicated.status_code == 200
    copied = duplicated.json()["definition"]
    assert copied["name"] == "copy-pipe"
    payload = (
        json.loads(copied["definition_json"])
        if isinstance(copied["definition_json"], str)
        else copied["definition_json"]
    )
    assert payload["name"] == "copy-pipe"

    exported = client.get(f"/api/pipelines/definitions/{row.id}/export")
    assert exported.status_code == 200
    assert "source-pipe" in exported.text

    imported = client.post(
        "/api/pipelines/definitions/import",
        json={
            "yaml_content": (
                "name: imported-pipe\ntype: pipeline\nsteps:\n  - id: s1\n    exec: echo hi\n"
            )
        },
    )
    assert imported.status_code == 200
    assert imported.json()["definition"]["name"] == "imported-pipe"

    pipe_manager.delete(row.id)
    restored = client.post(f"/api/pipelines/definitions/{row.id}/restore")
    assert restored.status_code == 200
    assert restored.json()["definition"]["name"] == "source-pipe"


def test_restore_from_template_and_moves(
    client: TestClient,
    pipe_manager: PipelineDefinitionManager,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    row = _create_pipeline(pipe_manager, name="tmpl-pipe")
    template_json = json.dumps(
        {
            "name": "tmpl-pipe",
            "type": "pipeline",
            "steps": [{"id": "restored", "exec": "echo restored"}],
        },
        sort_keys=True,
    )
    cache = TemplateHashCache()
    cache._hashes[("pipeline", row.name)] = compute_definition_hash(template_json)
    cache._json_cache[("pipeline", row.name)] = template_json
    from unittest.mock import patch

    with patch(
        "gobby.workflows.template_hashes.get_template_hash_cache",
        return_value=cache,
    ):
        resp = client.post(f"/api/pipelines/definitions/{row.id}/restore-from-template")
    assert resp.status_code == 200
    body = resp.json()["definition"]["definition_json"]
    payload = json.loads(body) if isinstance(body, str) else body
    assert payload["steps"][0]["id"] == "restored"

    project = LocalProjectManager(temp_db).create(name="pipe-project", repo_path=str(tmp_path))
    moved = client.post(
        f"/api/pipelines/definitions/{row.id}/move-to-project",
        json={"project_id": project.id},
    )
    assert moved.status_code == 200
    assert moved.json()["definition"]["project_id"] == project.id

    globalized = client.post(f"/api/pipelines/definitions/{row.id}/move-to-global")
    assert globalized.status_code == 200
    assert globalized.json()["definition"]["project_id"] is None


def test_create_project_override_when_global_exists(
    client: TestClient,
    pipe_manager: PipelineDefinitionManager,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    _create_pipeline(pipe_manager, name="shared-pipe")
    project = LocalProjectManager(temp_db).create(
        name="override-proj",
        repo_path=str(tmp_path),
    )
    resp = client.post(
        "/api/pipelines/definitions",
        json={
            "name": "shared-pipe",
            "project_id": project.id,
            "definition_json": json.dumps({**SAMPLE_PIPELINE, "name": "shared-pipe"}),
        },
    )
    assert resp.status_code == 200
    created = resp.json()["definition"]
    assert created["project_id"] == project.id
    assert created["name"] == "shared-pipe"


def test_unknown_definition_is_404(client: TestClient) -> None:
    assert client.get(f"/api/pipelines/definitions/{UNKNOWN_ID}").status_code == 404
    assert client.delete(f"/api/pipelines/definitions/{UNKNOWN_ID}").status_code == 404
    assert client.post(f"/api/pipelines/definitions/{UNKNOWN_ID}/restore").status_code == 404
