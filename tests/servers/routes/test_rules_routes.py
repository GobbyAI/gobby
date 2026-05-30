"""Tests for rule HTTP API routes.

Verifies rule-specific endpoints at /api/rules:
- GET /api/rules: list rules with event/group/enabled filters
- POST /api/rules: create a new rule (validates with RuleDefinitionBody)
- GET /api/rules/{name}: get rule by name
- PUT /api/rules/{name}: update rule fields
- DELETE /api/rules/{name}: soft-delete (bundled protected)
- PUT /api/rules/{name}/toggle: toggle enabled state
- GET /api/rules/groups: list distinct rule groups
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import pytest
from starlette.testclient import TestClient

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    return hub_db


@pytest.fixture
def def_manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def client(db: HubDatabase) -> TestClient:
    server = create_http_server(database=db)
    return TestClient(server.app)


def _seed_rule(
    def_manager: LocalWorkflowDefinitionManager,
    name: str = "test-rule",
    event: str = "before_tool",
    group: str = "test-group",
    enabled: bool = True,
    source: Literal["installed", "template", "agent", "project", "custom"] = "installed",
    tags: list[str] | None = None,
) -> str:
    """Seed a rule in the database and return its ID."""
    body = {
        "event": event,
        "group": group,
        "effect": {"type": "block", "reason": "test"},
    }
    row = def_manager.create(
        name=name,
        definition_json=json.dumps(body),
        workflow_type="rule",
        enabled=enabled,
        source=source,
        tags=tags,
    )
    return row.id


# ═══════════════════════════════════════════════════════════════════════
# GET /api/rules
# ═══════════════════════════════════════════════════════════════════════


class TestListRules:
    """GET /api/rules returns only rules with optional filters."""

    def test_list_all_rules(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="rule-a")
        _seed_rule(def_manager, name="rule-b")

        resp = client.get("/api/rules")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["count"] == 2
        names = [r["name"] for r in data["rules"]]
        assert "rule-a" in names
        assert "rule-b" in names

    def test_excludes_workflows(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="my-rule")
        def_manager.create(
            name="my-workflow",
            definition_json=json.dumps({"name": "my-workflow"}),
            workflow_type="workflow",
        )

        resp = client.get("/api/rules")
        data = resp.json()
        names = [r["name"] for r in data["rules"]]
        assert "my-rule" in names
        assert "my-workflow" not in names

    def test_filter_by_event(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="before-rule", event="before_tool")
        _seed_rule(def_manager, name="stop-rule", event="stop")

        resp = client.get("/api/rules", params={"event": "before_tool"})
        data = resp.json()
        names = [r["name"] for r in data["rules"]]
        assert "before-rule" in names
        assert "stop-rule" not in names

    def test_filter_by_group(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="alpha-rule", group="alpha")
        _seed_rule(def_manager, name="beta-rule", group="beta")

        resp = client.get("/api/rules", params={"group": "alpha"})
        data = resp.json()
        names = [r["name"] for r in data["rules"]]
        assert "alpha-rule" in names
        assert "beta-rule" not in names

    def test_filter_by_enabled(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="on-rule", enabled=True)
        _seed_rule(def_manager, name="off-rule", enabled=False)

        resp = client.get("/api/rules", params={"enabled": "true"})
        data = resp.json()
        names = [r["name"] for r in data["rules"]]
        assert "on-rule" in names
        assert "off-rule" not in names

    def test_empty_list(self, client: TestClient) -> None:
        resp = client.get("/api/rules")
        data = resp.json()
        assert data["status"] == "success"
        assert data["count"] == 0
        assert data["rules"] == []


# ═══════════════════════════════════════════════════════════════════════
# POST /api/rules
# ═══════════════════════════════════════════════════════════════════════


class TestCreateRule:
    """POST /api/rules creates a validated rule."""

    def test_creates_rule(self, client: TestClient) -> None:
        body = {
            "name": "new-rule",
            "definition": {
                "event": "before_tool",
                "group": "test",
                "effects": [{"type": "block", "reason": "testing"}],
            },
        }
        resp = client.post("/api/rules", json=body)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "success"
        assert data["rule"]["name"] == "new-rule"

    def test_rejects_invalid_definition(self, client: TestClient) -> None:
        body = {
            "name": "bad-rule",
            "definition": {"not_valid": True},
        }
        resp = client.post("/api/rules", json=body)
        assert resp.status_code == 400

    def test_rejects_duplicate_name(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="existing-rule")

        body = {
            "name": "existing-rule",
            "definition": {
                "event": "stop",
                "effects": [{"type": "block", "reason": "dup"}],
            },
        }
        resp = client.post("/api/rules", json=body)
        assert resp.status_code == 409


# ═══════════════════════════════════════════════════════════════════════
# GET /api/rules/{name}
# ═══════════════════════════════════════════════════════════════════════


class TestGetRule:
    """GET /api/rules/{name} returns full rule detail."""

    def test_returns_rule(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="my-rule", event="stop", group="test")

        resp = client.get("/api/rules/my-rule")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["rule"]["name"] == "my-rule"
        assert data["rule"]["event"] == "stop"
        assert data["rule"]["group"] == "test"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/rules/nonexistent")
        assert resp.status_code == 404

    def test_includes_enabled_status(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="disabled-rule", enabled=False)

        resp = client.get("/api/rules/disabled-rule")
        data = resp.json()
        assert data["rule"]["enabled"] is False


# ═══════════════════════════════════════════════════════════════════════
# PUT /api/rules/{name}
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateRule:
    """PUT /api/rules/{name} updates rule fields."""

    def test_update_priority(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="my-rule")

        resp = client.put("/api/rules/my-rule", json={"priority": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["rule"]["priority"] == 5

    def test_update_description(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="my-rule")

        resp = client.put("/api/rules/my-rule", json={"description": "Updated"})
        data = resp.json()
        assert data["rule"]["description"] == "Updated"

    def test_not_found(self, client: TestClient) -> None:
        resp = client.put("/api/rules/nonexistent", json={"priority": 5})
        assert resp.status_code == 404

    def test_no_fields(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="my-rule")

        resp = client.put("/api/rules/my-rule", json={})
        assert resp.status_code == 400

    def test_update_failure_logs_rule_context(
        self,
        client: TestClient,
        def_manager: LocalWorkflowDefinitionManager,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        row_id = _seed_rule(def_manager, name="my-rule")

        def fail_update(
            _self: LocalWorkflowDefinitionManager,
            _definition_id: str,
            **_fields: object,
        ) -> object:
            raise RuntimeError("write failed")

        monkeypatch.setattr(LocalWorkflowDefinitionManager, "update", fail_update)

        with caplog.at_level(logging.ERROR, logger="gobby.servers.routes.rules"):
            resp = client.put("/api/rules/my-rule", json={"priority": 5})

        assert resp.status_code == 500
        assert row_id in caplog.text
        assert "my-rule" in caplog.text
        assert "write failed" in caplog.text


# ═══════════════════════════════════════════════════════════════════════
# DELETE /api/rules/{name}
# ═══════════════════════════════════════════════════════════════════════


class TestDeleteRule:
    """DELETE /api/rules/{name} soft-deletes (bundled protected)."""

    def test_deletes_custom_rule(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="custom-rule", tags=["user"])

        resp = client.delete("/api/rules/custom-rule")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"

    def test_protects_bundled_rule(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="bundled-rule", tags=["gobby", "default"])

        resp = client.delete("/api/rules/bundled-rule")
        assert resp.status_code == 403

    def test_force_deletes_bundled(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="bundled-rule", tags=["gobby", "default"])

        resp = client.delete("/api/rules/bundled-rule", params={"force": "true"})
        assert resp.status_code == 200

    def test_not_found(self, client: TestClient) -> None:
        resp = client.delete("/api/rules/nonexistent")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# PUT /api/rules/{name}/toggle
# ═══════════════════════════════════════════════════════════════════════


class TestToggleRule:
    """PUT /api/rules/{name}/toggle toggles enabled state."""

    def test_disable_rule(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="my-rule", enabled=True)

        resp = client.put("/api/rules/my-rule/toggle", json={"enabled": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["rule"]["enabled"] is False

    def test_enable_rule(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="my-rule", enabled=False)

        resp = client.put("/api/rules/my-rule/toggle", json={"enabled": True})
        data = resp.json()
        assert data["rule"]["enabled"] is True

    def test_not_found(self, client: TestClient) -> None:
        resp = client.put("/api/rules/nonexistent/toggle", json={"enabled": True})
        assert resp.status_code == 404


class TestBulkToggleRules:
    """PUT /api/rules/bulk-toggle updates matching rules best-effort."""

    def test_continues_after_single_rule_update_failure(
        self,
        client: TestClient,
        def_manager: LocalWorkflowDefinitionManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failing_rule_id = _seed_rule(def_manager, name="installed-a", source="installed")
        successful_rule_id = _seed_rule(def_manager, name="installed-b", source="installed")
        project_rule_id = _seed_rule(def_manager, name="project-a", source="project")
        original_update = LocalWorkflowDefinitionManager.update

        def flaky_update(
            manager: LocalWorkflowDefinitionManager,
            definition_id: str,
            **fields: Any,
        ) -> Any:
            if definition_id == failing_rule_id:
                raise RuntimeError("row update failed")
            return original_update(manager, definition_id, **fields)

        monkeypatch.setattr(LocalWorkflowDefinitionManager, "update", flaky_update)

        response = client.put(
            "/api/rules/bulk-toggle",
            json={"source": "installed", "enabled": False},
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "success",
            "count": 1,
            "failures": [
                {
                    "rule_id": failing_rule_id,
                    "rule_name": "installed-a",
                    "source": "installed",
                    "error": "row update failed",
                }
            ],
            "partial": True,
        }
        assert def_manager.get(failing_rule_id).enabled is True
        assert def_manager.get(successful_rule_id).enabled is False
        assert def_manager.get(project_rule_id).enabled is True


# ═══════════════════════════════════════════════════════════════════════
# GET /api/rules/groups
# ═══════════════════════════════════════════════════════════════════════


class TestListGroups:
    """GET /api/rules/groups returns distinct rule groups."""

    def test_returns_groups(
        self, client: TestClient, def_manager: LocalWorkflowDefinitionManager
    ) -> None:
        _seed_rule(def_manager, name="rule-a", group="alpha")
        _seed_rule(def_manager, name="rule-b", group="beta")
        _seed_rule(def_manager, name="rule-c", group="alpha")

        resp = client.get("/api/rules/groups")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert set(data["groups"]) == {"alpha", "beta"}

    def test_empty_groups(self, client: TestClient) -> None:
        resp = client.get("/api/rules/groups")
        data = resp.json()
        assert data["groups"] == []
