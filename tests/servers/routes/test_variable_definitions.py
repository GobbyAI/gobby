"""HTTP tests for /api/variables (plan 5.1)."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.definitions._shared import compute_definition_hash
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.workflows.template_hashes import TemplateHashCache
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

UNKNOWN_ID = "99999999-9999-4999-8999-999999999999"


@pytest.fixture
def var_manager(temp_db: HubDatabase) -> SessionVariableDefaultManager:
    return SessionVariableDefaultManager(temp_db)


@pytest.fixture
def client(temp_db: HubDatabase) -> TestClient:
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
    )
    return TestClient(server.app)


def test_variable_definitions_router_is_importable() -> None:
    module = importlib.import_module("gobby.servers.routes.variable_definitions")
    assert callable(module.create_variable_definitions_router)


def test_list_empty(client: TestClient) -> None:
    resp = client.get("/api/variables")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["count"] == 0
    assert data["variables"] == []


def test_create_update_toggle_delete_cycle(client: TestClient) -> None:
    created = client.post(
        "/api/variables",
        json={"name": "cycle_var", "value": "one", "description": "cycle"},
    )
    assert created.status_code == 200
    variable = created.json()["variable"]
    assert variable["name"] == "cycle_var"
    assert variable["value"] == "one"

    updated = client.put(
        "/api/variables/cycle_var",
        json={"value": "two", "description": "updated"},
    )
    assert updated.status_code == 200
    assert updated.json()["variable"]["value"] == "two"

    toggled = client.put("/api/variables/cycle_var/toggle")
    assert toggled.status_code == 200
    assert toggled.json()["variable"]["enabled"] is False

    deleted = client.delete("/api/variables/cycle_var")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    listed = client.get("/api/variables")
    names = {row["name"] for row in listed.json()["variables"]}
    assert "cycle_var" not in names


def test_restore_from_template_uses_kind_cache(
    client: TestClient,
    var_manager: SessionVariableDefaultManager,
) -> None:
    row = var_manager.create(name="tmpl_var", default_value="old", source="installed")
    template_json = '{"description": null, "value": "bundled", "variable": "tmpl_var"}'
    cache = TemplateHashCache()
    cache._hashes[("variable", row.name)] = compute_definition_hash(template_json)
    cache._json_cache[("variable", row.name)] = template_json
    with patch(
        "gobby.workflows.template_hashes.get_template_hash_cache",
        return_value=cache,
    ):
        resp = client.post(f"/api/variables/{row.id}/restore-from-template")
    assert resp.status_code == 200
    assert resp.json()["variable"]["value"] == "bundled"


def test_create_project_override_when_global_exists(
    client: TestClient,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    global_resp = client.post(
        "/api/variables",
        json={"name": "shared_var", "value": "global"},
    )
    assert global_resp.status_code == 200
    project = LocalProjectManager(temp_db).create(
        name="var-override-proj",
        repo_path=str(tmp_path),
    )
    resp = client.post(
        "/api/variables",
        json={"name": "shared_var", "value": "project", "project_id": project.id},
    )
    assert resp.status_code == 200
    created = resp.json()["variable"]
    assert created["name"] == "shared_var"
    assert created["project_id"] == project.id
    assert created["value"] == "project"


def test_unknown_variable_is_404(client: TestClient) -> None:
    assert client.put(f"/api/variables/{UNKNOWN_ID}", json={"value": 1}).status_code == 404
    assert client.delete(f"/api/variables/{UNKNOWN_ID}").status_code == 404
    assert client.post(f"/api/variables/{UNKNOWN_ID}/restore-from-template").status_code == 404
