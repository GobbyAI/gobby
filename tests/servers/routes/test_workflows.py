"""Tests for workflow definition routes.

Exercises src/gobby/servers/routes/workflows.py endpoints using
create_http_server() with a real LocalWorkflowDefinitionManager backed by temp_db.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


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
        "definition_json": RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            effects=[RuleEffect(type="block", reason="test")],
        ).model_dump_json(),
        "workflow_type": "rule",
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
        _create_workflow(wf_manager, name="wf-rule", workflow_type="rule")
        _create_workflow(wf_manager, name="wf-pipe", workflow_type="pipeline", definition_json="{}")
        resp = client.get("/api/workflows?workflow_type=rule")
        data = resp.json()
        assert data["count"] == 1
        assert data["definitions"][0]["name"] == "wf-rule"

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
        resp = client.get("/api/workflows/nonexistent-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/workflows
# ---------------------------------------------------------------------------


class TestCreateWorkflow:
    def test_create_success(self, client: TestClient) -> None:
        body = {
            "name": "new-workflow",
            "definition_json": RuleDefinitionBody(
                event=RuleEvent.STOP,
                effects=[RuleEffect(type="block", reason="stop")],
            ).model_dump_json(),
            "workflow_type": "rule",
        }
        resp = client.post("/api/workflows", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["definition"]["name"] == "new-workflow"


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
        resp = client.put("/api/workflows/nonexistent", json={"name": "x"})
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
        resp = client.put("/api/workflows/nonexistent/toggle")
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
        resp = client.delete("/api/workflows/nonexistent")
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
            "/api/workflows/nonexistent/duplicate",
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
        resp = client.get("/api/workflows/nonexistent/export")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/workflows/import
# ---------------------------------------------------------------------------


class TestImportWorkflow:
    def test_import_invalid_yaml(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows/import",
            json={"yaml_content": "not: valid: yaml: [[["},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/workflows/{id}/restore
# ---------------------------------------------------------------------------


class TestRestoreWorkflow:
    def test_restore_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/workflows/nonexistent/restore")
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
                    "value": ["task-transitions", "verification-before-completion"],
                    "session_id": "#1",
                },
            )

        assert resp.status_code == 200
        mock_set.assert_called_once()
        assert mock_set.call_args.kwargs["value"] == [
            "task-transitions",
            "verification-before-completion",
        ]

    def test_set_variable_accepts_workflow_scope(self) -> None:
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
                    "workflow": "backend-developer-steps",
                },
            )

        assert resp.status_code == 200
        mock_set.assert_called_once()
        assert mock_set.call_args.kwargs["workflow"] == "backend-developer-steps"

    def test_get_variable_accepts_workflow_scope(self) -> None:
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
                    "workflow": "backend-developer-steps",
                },
            )

        assert resp.status_code == 200
        mock_get.assert_called_once()
        assert mock_get.call_args.kwargs["workflow"] == "backend-developer-steps"

    def test_get_variable_with_session_manager(self, temp_db) -> None:
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
# Removed legacy install routes
# ---------------------------------------------------------------------------


class TestRemovedInstallRoutes:
    def test_install_all_templates_route_removed(self, client: TestClient) -> None:
        resp = client.post("/api/workflows/install-all-templates")
        assert resp.status_code == 404

    def test_install_from_template_route_removed(self, client: TestClient) -> None:
        resp = client.post("/api/workflows/workflow-id/install")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/workflows/{id}/move-to-project and move-to-global
# ---------------------------------------------------------------------------


class TestMoveWorkflow:
    def test_move_to_project_not_found(self, client: TestClient) -> None:
        resp = client.post(
            "/api/workflows/nonexistent/move-to-project",
            json={"project_id": "proj-1"},
        )
        assert resp.status_code == 404

    def test_move_to_global_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/workflows/nonexistent/move-to-global")
        assert resp.status_code == 404
