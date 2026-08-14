"""Tests for workflow definition routes.

Exercises src/gobby/servers/routes/workflows.py endpoints using
create_http_server() with a real LocalWorkflowDefinitionManager backed by temp_db.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import (
    LocalWorkflowDefinitionManager,
    compute_definition_hash,
)
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.pipeline_loader import PipelineLoader
from gobby.workflows.template_hashes import TemplateHashCache
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

# Valid-format UUIDs that don't exist in the database.
UNKNOWN_ID = "99999999-9999-4999-8999-999999999999"
PROJECT_ID = "11111111-1111-4111-8111-111111111111"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def wf_manager(temp_db) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(temp_db)


@pytest.fixture
def server(temp_db):
    srv = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
    )
    return srv


@pytest.fixture
def client(server) -> TestClient:
    return TestClient(server.app)


def _create_workflow(wf_manager: LocalWorkflowDefinitionManager, **kwargs) -> dict:
    defaults = {
        "name": "test-workflow",
        "definition_json": json.dumps({"name": "test-workflow", "type": "step", "steps": []}),
        "workflow_type": "workflow",
    }
    defaults.update(kwargs)
    row = wf_manager.create(**defaults)
    return row.to_dict()


# ---------------------------------------------------------------------------
# GET /api/workflows
# ---------------------------------------------------------------------------


class TestListWorkflows:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/api/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["count"] == 0

    def test_list_with_entries(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _create_workflow(wf_manager, name="wf-1")
        _create_workflow(wf_manager, name="wf-2")
        resp = client.get("/api/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2

    def test_list_filter_by_type(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _create_workflow(wf_manager, name="wf-step", workflow_type="workflow")
        _create_workflow(wf_manager, name="wf-pipe", workflow_type="pipeline", definition_json="{}")
        resp = client.get("/api/workflows?workflow_type=pipeline")
        assert resp.status_code == 400
        assert "pipeline domain MCP tools" in resp.json()["detail"]

    def test_list_filter_by_enabled(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _create_workflow(wf_manager, name="wf-on", enabled=True)
        _create_workflow(wf_manager, name="wf-off", enabled=False)
        resp = client.get("/api/workflows?enabled=true")
        data = resp.json()
        assert data["count"] == 1
        assert data["definitions"][0]["name"] == "wf-on"


# ---------------------------------------------------------------------------
# GET /api/workflows/{id}
# ---------------------------------------------------------------------------


class TestGetWorkflow:
    def test_get_existing(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf = _create_workflow(wf_manager)
        resp = client.get(f"/api/workflows/{wf['id']}")
        assert resp.status_code == 200
        assert resp.json()["definition"]["name"] == "test-workflow"

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get(f"/api/workflows/{UNKNOWN_ID}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/workflows
# ---------------------------------------------------------------------------


class TestCreateWorkflow:
    def test_create_success(self, client: TestClient) -> None:
        body = {
            "name": "new-workflow",
            "definition_json": json.dumps({"name": "new-workflow", "type": "step", "steps": []}),
            "workflow_type": "workflow",
        }
        resp = client.post("/api/workflows", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["definition"]["name"] == "new-workflow"

    def test_create_rejects_rule_kind(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows",
            json={
                "name": "rogue-rule",
                "definition_json": RuleDefinitionBody(
                    event=RuleTriggerEvent.STOP,
                    effects=[RuleEffect(type="block", reason="stop")],
                ).model_dump_json(),
                "workflow_type": "rule",
            },
        )
        assert resp.status_code == 400
        assert "/api/rules" in resp.json()["detail"]

    def test_create_rejects_agent_kind(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows",
            json={
                "name": "rogue-agent",
                "definition_json": '{"name": "rogue-agent", "provider": "claude"}',
                "workflow_type": "agent",
            },
        )
        assert resp.status_code == 400
        assert "/api/agents" in resp.json()["detail"]

    def test_list_omits_and_rejects_agent_filter(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf_manager.create(
            name="hidden-agent",
            definition_json='{"name": "hidden-agent"}',
            workflow_type="agent",
            source="installed",
        )
        listed = client.get("/api/workflows")
        assert listed.status_code == 200
        names = [row["name"] for row in listed.json()["definitions"]]
        assert "hidden-agent" not in names
        filtered = client.get("/api/workflows", params={"workflow_type": "agent"})
        assert filtered.status_code == 400
        assert "/api/agents" in filtered.json()["detail"]

    def test_list_omits_and_rejects_rule_filter(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf_manager.create(
            name="hidden-rule",
            definition_json='{"event": "stop", "effects": []}',
            workflow_type="rule",
            source="installed",
        )
        listed = client.get("/api/workflows")
        assert listed.status_code == 200
        names = [row["name"] for row in listed.json()["definitions"]]
        assert "hidden-rule" not in names
        filtered = client.get("/api/workflows", params={"workflow_type": "rule"})
        assert filtered.status_code == 400
        assert "/api/rules" in filtered.json()["detail"]

    def test_create_rejects_variable_kind(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows",
            json={
                "name": "rogue-variable",
                "definition_json": '{"variable": "rogue-variable", "value": 1}',
                "workflow_type": "variable",
            },
        )
        assert resp.status_code == 400
        assert "variable domain MCP tools" in resp.json()["detail"]

    def test_create_rejects_pipeline_kind(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows",
            json={
                "name": "rogue-pipeline",
                "definition_json": (
                    '{"name": "rogue-pipeline", "type": "pipeline", '
                    '"steps": [{"id": "s1", "exec": "echo hi"}]}'
                ),
                "workflow_type": "pipeline",
            },
        )
        assert resp.status_code == 400
        assert "pipeline domain MCP tools" in resp.json()["detail"]

    def test_list_omits_and_rejects_variable_filter(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf_manager.create(
            name="hidden-variable",
            definition_json='{"variable": "hidden-variable", "value": 1}',
            workflow_type="variable",
            source="installed",
        )
        listed = client.get("/api/workflows")
        assert listed.status_code == 200
        names = [row["name"] for row in listed.json()["definitions"]]
        assert "hidden-variable" not in names
        filtered = client.get("/api/workflows", params={"workflow_type": "variable"})
        assert filtered.status_code == 400
        assert "variable domain MCP tools" in filtered.json()["detail"]

    def test_list_omits_and_rejects_pipeline_filter(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf_manager.create(
            name="hidden-pipeline",
            definition_json='{"name": "hidden-pipeline", "type": "pipeline", "steps": []}',
            workflow_type="pipeline",
            source="installed",
        )
        listed = client.get("/api/workflows")
        assert listed.status_code == 200
        names = [row["name"] for row in listed.json()["definitions"]]
        assert "hidden-pipeline" not in names
        filtered = client.get("/api/workflows", params={"workflow_type": "pipeline"})
        assert filtered.status_code == 400
        assert "pipeline domain MCP tools" in filtered.json()["detail"]

    @pytest.mark.parametrize(
        "definition_json",
        ["not-json", '{"unexpected": true}'],
    )
    def test_rejects_invalid_rule_definition(
        self, client: TestClient, definition_json: str
    ) -> None:
        resp = client.post(
            "/api/workflows",
            json={
                "name": "invalid-rule",
                "definition_json": definition_json,
                "workflow_type": "rule",
            },
        )

        assert resp.status_code == 400

    def test_rejects_duplicate_name(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        existing = _create_workflow(wf_manager)

        resp = client.post(
            "/api/workflows",
            json={
                "name": existing["name"],
                "definition_json": existing["definition_json"],
                "workflow_type": "workflow",
            },
        )

        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# PUT /api/workflows/{id}
# ---------------------------------------------------------------------------


class TestUpdateWorkflow:
    def test_update_success(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf = _create_workflow(wf_manager)
        resp = client.put(f"/api/workflows/{wf['id']}", json={"description": "updated"})
        assert resp.status_code == 200
        assert resp.json()["definition"]["description"] == "updated"

    def test_update_no_fields(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf = _create_workflow(wf_manager)
        resp = client.put(f"/api/workflows/{wf['id']}", json={})
        assert resp.status_code == 400
        assert "No fields" in resp.json()["detail"]

    def test_update_not_found(self, client: TestClient) -> None:
        resp = client.put(f"/api/workflows/{UNKNOWN_ID}", json={"name": "x"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/workflows/{id}/toggle
# ---------------------------------------------------------------------------


class TestToggleWorkflow:
    def test_toggle(self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager) -> None:
        wf = _create_workflow(wf_manager, enabled=True)
        resp = client.put(f"/api/workflows/{wf['id']}/toggle")
        assert resp.status_code == 200
        assert resp.json()["definition"]["enabled"] is False

    def test_toggle_not_found(self, client: TestClient) -> None:
        resp = client.put(f"/api/workflows/{UNKNOWN_ID}/toggle")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/workflows/{id}
# ---------------------------------------------------------------------------


class TestDeleteWorkflow:
    def test_delete_success(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf = _create_workflow(wf_manager)
        resp = client.delete(f"/api/workflows/{wf['id']}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_not_found(self, client: TestClient) -> None:
        resp = client.delete(f"/api/workflows/{UNKNOWN_ID}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/workflows/{id}/duplicate
# ---------------------------------------------------------------------------


class TestDuplicateWorkflow:
    def test_duplicate_success(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf = _create_workflow(wf_manager)
        resp = client.post(
            f"/api/workflows/{wf['id']}/duplicate",
            json={"new_name": "copy-of-workflow"},
        )
        assert resp.status_code == 200
        assert resp.json()["definition"]["name"] == "copy-of-workflow"

    def test_duplicate_not_found(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/workflows/{UNKNOWN_ID}/duplicate",
            json={"new_name": "copy"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/workflows/{id}/export
# ---------------------------------------------------------------------------


class TestExportWorkflow:
    def test_export_success(
        self, client: TestClient, wf_manager: LocalWorkflowDefinitionManager
    ) -> None:
        wf = _create_workflow(wf_manager)
        resp = client.get(f"/api/workflows/{wf['id']}/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-yaml")

    def test_export_not_found(self, client: TestClient) -> None:
        resp = client.get(f"/api/workflows/{UNKNOWN_ID}/export")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/workflows/import
# ---------------------------------------------------------------------------


class TestImportWorkflow:
    @pytest.mark.parametrize(
        ("enabled_yaml", "expected_enabled"),
        [("", True), ('enabled: "false"\n', False)],
    )
    def test_import_normalizes_enabled(
        self,
        client: TestClient,
        wf_manager: LocalWorkflowDefinitionManager,
        enabled_yaml: str,
        expected_enabled: bool,
    ) -> None:
        resp = client.post(
            "/api/workflows/import",
            json={
                "yaml_content": f"""\
name: imported-pipeline
type: pipeline
{enabled_yaml}steps:
  - id: run
    exec: echo ok
""",
            },
        )

        assert resp.status_code == 400
        assert "pipeline domain MCP tools" in resp.json()["detail"]

    def test_import_invalid_yaml(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows/import",
            json={"yaml_content": "not: valid: yaml: [[["},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/workflows/{id}/restore
# ---------------------------------------------------------------------------


class TestRestoreFromTemplate:
    def test_same_named_pipeline_template_does_not_restore_rule(
        self,
        client: TestClient,
        wf_manager: LocalWorkflowDefinitionManager,
    ) -> None:
        created = _create_workflow(wf_manager, name="shared-name")
        original_json = created["definition_json"]
        pipeline_json = '{"name":"shared-name","type":"pipeline"}'
        cache = TemplateHashCache()
        cache._hashes[("pipeline", "shared-name")] = compute_definition_hash(pipeline_json)
        cache._json_cache[("pipeline", "shared-name")] = pipeline_json

        with patch(
            "gobby.workflows.template_hashes.get_template_hash_cache",
            return_value=cache,
        ):
            list_resp = client.get("/api/workflows")
            resp = client.post(f"/api/workflows/{created['id']}/restore-from-template")

        listed_definition = list_resp.json()["definitions"][0]
        assert listed_definition["has_template_update"] is False
        assert resp.status_code == 200
        assert resp.json()["message"] == "Definition already matches template"
        assert wf_manager.get(created["id"]).definition_json == original_json


class TestRestoreWorkflow:
    def test_restore_not_found(self, client: TestClient) -> None:
        resp = client.post(f"/api/workflows/{UNKNOWN_ID}/restore")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/workflows/variables/set and /get
# ---------------------------------------------------------------------------


class TestVariables:
    def test_set_variable_no_session_manager(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows/variables/set",
            json={"name": "foo", "value": "bar", "session_id": "#1"},
        )
        assert resp.status_code == 503

    def test_get_variable_no_session_manager(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows/variables/get",
            json={"name": "foo", "session_id": "#1"},
        )
        assert resp.status_code == 503

    def test_set_variable_missing_session_id(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows/variables/set",
            json={"name": "foo", "value": "bar"},
        )
        assert resp.status_code == 422

    def test_get_variable_missing_session_id(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows/variables/get",
            json={"name": "foo"},
        )
        assert resp.status_code == 422

    def test_set_variable_with_session_manager(self, temp_db) -> None:
        mock_sm = MagicMock()
        mock_sm.db = temp_db
        srv = create_http_server(
            config=DaemonConfig(),
            database=temp_db,
            session_manager=mock_sm,
        )
        c = TestClient(srv.app)
        with patch(
            "gobby.mcp_proxy.tools.workflows._variables.set_variable",
            return_value={"success": True},
        ):
            resp = c.post(
                "/api/workflows/variables/set",
                json={"name": "foo", "value": "bar", "session_id": "#1"},
            )
        assert resp.status_code == 200

    def test_set_variable_accepts_json_values(self) -> None:
        mock_sm = MagicMock()
        mock_sm.db = MagicMock()
        srv = create_http_server(
            config=DaemonConfig(),
            database=mock_sm.db,
            session_manager=mock_sm,
        )
        c = TestClient(srv.app)
        with patch(
            "gobby.mcp_proxy.tools.workflows._variables.set_variable",
            return_value={"success": True},
        ) as mock_set:
            resp = c.post(
                "/api/workflows/variables/set",
                json={
                    "name": "loaded_skills",
                    "value": ["tasks"],
                    "session_id": "#1",
                },
            )

        assert resp.status_code == 200
        mock_set.assert_called_once()
        assert mock_set.call_args.kwargs["value"] == ["tasks"]

    def test_set_variable_accepts_step_scope(self) -> None:
        mock_sm = MagicMock()
        mock_sm.db = MagicMock()
        srv = create_http_server(
            config=DaemonConfig(),
            database=mock_sm.db,
            session_manager=mock_sm,
        )
        c = TestClient(srv.app)
        with patch(
            "gobby.mcp_proxy.tools.workflows._variables.set_variable",
            return_value={"success": True},
        ) as mock_set:
            resp = c.post(
                "/api/workflows/variables/set",
                json={
                    "name": "implementation_complete",
                    "value": True,
                    "session_id": "#1",
                    "scope": "step",
                },
            )

        assert resp.status_code == 200
        mock_set.assert_called_once()
        assert mock_set.call_args.kwargs["scope"] == "step"

    def test_get_variable_accepts_step_scope(self) -> None:
        mock_sm = MagicMock()
        mock_sm.db = MagicMock()
        srv = create_http_server(
            config=DaemonConfig(),
            database=mock_sm.db,
            session_manager=mock_sm,
        )
        c = TestClient(srv.app)
        with patch(
            "gobby.mcp_proxy.tools.workflows._variables.get_variable",
            return_value={"success": True, "value": True},
        ) as mock_get:
            resp = c.post(
                "/api/workflows/variables/get",
                json={
                    "name": "implementation_complete",
                    "session_id": "#1",
                    "scope": "step",
                },
            )

        assert resp.status_code == 200
        mock_get.assert_called_once()
        assert mock_get.call_args.kwargs["scope"] == "step"

    def test_get_variable_with_session_manager(self, temp_db: HubDatabase) -> None:
        mock_sm = MagicMock()
        mock_sm.db = temp_db
        srv = create_http_server(
            config=DaemonConfig(),
            database=temp_db,
            session_manager=mock_sm,
        )
        c = TestClient(srv.app)
        with patch(
            "gobby.mcp_proxy.tools.workflows._variables.get_variable",
            return_value={"success": True, "value": None},
        ):
            resp = c.post(
                "/api/workflows/variables/get",
                json={"name": "foo", "session_id": "#1"},
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/workflows/templates
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_list_templates(self, client: TestClient) -> None:
        resp = client.get("/api/workflows/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "templates" in data


# ---------------------------------------------------------------------------
# POST /api/workflows/{id}/move-to-project and move-to-global
# ---------------------------------------------------------------------------


class TestMoveWorkflow:
    def test_move_to_project_not_found(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/workflows/{UNKNOWN_ID}/move-to-project",
            json={"project_id": PROJECT_ID},
        )
        assert resp.status_code == 404

    def test_move_to_global_not_found(self, client: TestClient) -> None:
        resp = client.post(f"/api/workflows/{UNKNOWN_ID}/move-to-global")
        assert resp.status_code == 404
